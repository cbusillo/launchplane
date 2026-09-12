import hashlib
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    PositiveInt,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import CoreSchema


MergeTrainActorRole = Literal["repo_owner", "repo_admin"]
MergeTrainFailurePolicy = Literal["pause_train", "continue_after_blocking_pr"]
MergeTrainIdentityKind = Literal["github_actions_oidc", "github_app", "github_token_secret"]
MergeTrainMergeMethod = Literal["merge", "squash", "rebase"]
MergeTrainEngineeringReviewMode = Literal["advisory", "required"]
MergeTrainPolicyRecordStatus = Literal["active", "superseded"]
MergeTrainSchedulerRunnerMode = Literal["level1", "controller"]
MERGE_TRAIN_POLICY_TARGETS_READ_ACTION = "merge_train.policy_targets"
_PROVIDER_DELIVERY_ALLOWED_MERGE_METHODS = ("merge", "squash", "rebase")


MergeTrainPolicyCompareWriteStatus = Literal[
    "written",
    "unchanged",
    "stale",
    "missing",
    "ambiguous_active",
    "record_id_conflict",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]


class MergeTrainPolicyCompareWriteResult(NamedTuple):
    status: MergeTrainPolicyCompareWriteStatus
    current_record: "MergeTrainPolicyRecord | None" = None
    idempotency_record: object | None = None


def normalize_merge_train_policy_timestamp(value: str) -> str:
    normalized_value = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise ValueError("merge train policy timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError("merge train policy timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


class MergeTrainEnqueuePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_required: bool = True
    allowed_actor_roles: tuple[MergeTrainActorRole, ...] = ("repo_owner", "repo_admin")
    trusted_automation_github_user_ids: tuple[PositiveInt, ...] = ()

    @model_validator(mode="after")
    def _validate_enqueue_policy(self) -> "MergeTrainEnqueuePolicy":
        if not self.allowed_actor_roles:
            raise ValueError("merge train enqueue policy requires at least one actor role")
        self.allowed_actor_roles = tuple(dict.fromkeys(self.allowed_actor_roles))
        self.trusted_automation_github_user_ids = tuple(
            sorted(set(self.trusted_automation_github_user_ids))
        )
        return self

    @model_serializer(mode="wrap")
    def _serialize_enqueue_policy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = cast(dict[str, Any], handler(self))
        if not self.trusted_automation_github_user_ids:
            payload.pop("trusted_automation_github_user_ids", None)
        return payload


class MergeTrainIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MergeTrainIdentityKind
    name: str

    @model_validator(mode="after")
    def _validate_identity(self) -> "MergeTrainIdentity":
        if not self.name.strip():
            raise ValueError("merge train identity requires name")
        self.name = self.name.strip()
        return self


class MergeTrainServiceAuthz(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = "merge_train.run_once"
    product: str = "launchplane"
    context: str = "launchplane"

    @model_validator(mode="after")
    def _validate_service_authz(self) -> "MergeTrainServiceAuthz":
        self.action = _normalize_required_value(
            self.action, "merge train service authz requires action"
        )
        self.product = _normalize_required_value(
            self.product, "merge train service authz requires product"
        )
        self.context = _normalize_required_value(
            self.context, "merge train service authz requires context"
        )
        return self


class MergeTrainGitHubTokenSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_var: str = ""

    @model_validator(mode="after")
    def _validate_token_source(self) -> "MergeTrainGitHubTokenSource":
        self.env_var = self.env_var.strip()
        return self


class MergeTrainSchedulerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    runner_mode: MergeTrainSchedulerRunnerMode = "controller"
    mutate: bool = False


class ProviderRequiredStatusCheckExpectationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: str = Field(min_length=1, max_length=512)
    app_id: int = Field(strict=True, gt=0, le=2**63 - 1)

    @model_validator(mode="after")
    def _normalize_status_check(self) -> "ProviderRequiredStatusCheckExpectationV1":
        object.__setattr__(
            self,
            "context",
            _normalize_required_value(
                self.context,
                "provider required status check requires context",
            ),
        )
        return self


class ProviderCodeScanningToolExpectationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1, max_length=512)
    alerts_threshold: Literal["none", "errors", "errors_and_warnings", "all"]
    security_alerts_threshold: Literal[
        "none",
        "critical",
        "high_or_higher",
        "medium_or_higher",
        "all",
    ]

    @model_validator(mode="after")
    def _normalize_scanning_tool(self) -> "ProviderCodeScanningToolExpectationV1":
        object.__setattr__(
            self,
            "tool",
            _normalize_required_value(
                self.tool,
                "provider code scanning expectation requires tool",
            ),
        )
        return self


class ProviderPullRequestExpectationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dismiss_stale_reviews_on_push: bool
    require_code_owner_review: bool
    require_last_push_approval: bool
    required_approving_review_count: int = Field(strict=True, ge=0, le=6)
    required_review_thread_resolution: bool


class ProviderDeliveryProtectionExpectationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_status_checks: tuple[ProviderRequiredStatusCheckExpectationV1, ...] = Field(
        min_length=1,
        max_length=100,
    )
    strict_required_status_checks_policy: bool
    code_scanning_tools: tuple[ProviderCodeScanningToolExpectationV1, ...] = Field(
        max_length=32,
    )
    pull_request: ProviderPullRequestExpectationV1 | None
    allowed_merge_methods: tuple[MergeTrainMergeMethod, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @field_validator("required_status_checks")
    @classmethod
    def _normalize_required_status_checks(
        cls,
        value: tuple[ProviderRequiredStatusCheckExpectationV1, ...],
    ) -> tuple[ProviderRequiredStatusCheckExpectationV1, ...]:
        ordered = tuple(sorted(value, key=lambda item: (item.context.casefold(), item.app_id)))
        identities = tuple((item.context.casefold(), item.app_id) for item in ordered)
        if len(identities) != len(set(identities)):
            raise ValueError("provider required status checks must be unique by context/app_id")
        return ordered

    @field_validator("code_scanning_tools")
    @classmethod
    def _normalize_code_scanning_tools(
        cls,
        value: tuple[ProviderCodeScanningToolExpectationV1, ...],
    ) -> tuple[ProviderCodeScanningToolExpectationV1, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.tool))
        names = tuple(item.tool for item in ordered)
        if len(names) != len(set(names)):
            raise ValueError("provider code scanning expectations must use unique tool names")
        return ordered

    @field_validator("allowed_merge_methods")
    @classmethod
    def _normalize_allowed_merge_methods(
        cls,
        value: tuple[MergeTrainMergeMethod, ...],
    ) -> tuple[MergeTrainMergeMethod, ...]:
        if len(value) != len(set(value)):
            raise ValueError("provider allowed merge methods must be unique")
        if "merge" not in value:
            raise ValueError("provider allowed merge methods must include merge")
        return tuple(
            method for method in _PROVIDER_DELIVERY_ALLOWED_MERGE_METHODS if method in value
        )

    @model_serializer(mode="wrap")
    def _serialize_expectation(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        payload = cast(dict[str, Any], handler(self))
        if self.pull_request is None:
            payload["pull_request"] = None
        return payload

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> dict[str, Any]:
        # The wrap serializer preserves a required explicit null when a containing
        # storage model uses exclude_none. Generate the output schema from the
        # model fields so that serializer implementation detail does not erase it.
        schema_without_serializer = dict(core_schema)
        schema_without_serializer.pop("serialization", None)
        return handler(cast(CoreSchema, schema_without_serializer))


class MergeTrainRepositoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    enqueue_label: str
    blocked_label: str
    stack_child_disposition_label: str = ""
    merge_method: MergeTrainMergeMethod
    engineering_review_mode: MergeTrainEngineeringReviewMode = "advisory"
    failure_policy: MergeTrainFailurePolicy
    enqueue: MergeTrainEnqueuePolicy
    merge_identity: MergeTrainIdentity
    service_authz: MergeTrainServiceAuthz = Field(default_factory=MergeTrainServiceAuthz)
    github_token: MergeTrainGitHubTokenSource = Field(default_factory=MergeTrainGitHubTokenSource)
    scheduler: MergeTrainSchedulerPolicy = Field(default_factory=MergeTrainSchedulerPolicy)
    provider_delivery_protection_expectation: ProviderDeliveryProtectionExpectationV1 | None = (
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            json_schema_extra={"x-launchplane-optional-response": True},
        )
    )

    @model_validator(mode="after")
    def _validate_repository_policy(self) -> "MergeTrainRepositoryPolicy":
        self.repository = _normalize_required_value(
            self.repository, "merge train policy requires repository"
        )
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train policy requires base_branch"
        )
        self.enqueue_label = _normalize_required_value(
            self.enqueue_label, "merge train policy requires enqueue_label"
        )
        self.blocked_label = _normalize_required_value(
            self.blocked_label, "merge train policy requires blocked_label"
        )
        self.stack_child_disposition_label = self.stack_child_disposition_label.strip()
        if self.enqueue_label == self.blocked_label:
            raise ValueError("merge train enqueue_label and blocked_label must differ")
        if self.stack_child_disposition_label in {
            self.enqueue_label,
            self.blocked_label,
        }:
            raise ValueError(
                "merge train stack_child_disposition_label must differ from enqueue_label and blocked_label"
            )
        return self

    @property
    def policy_key(self) -> str:
        return f"{self.repository}:{self.base_branch}"


class MergeTrainPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    policies: tuple[MergeTrainRepositoryPolicy, ...]

    @model_validator(mode="after")
    def _validate_policy(self) -> "MergeTrainPolicy":
        if not self.policies:
            raise ValueError("merge train policy requires at least one repository policy")
        seen_keys: set[str] = set()
        for repository_policy in self.policies:
            if repository_policy.policy_key in seen_keys:
                raise ValueError("merge train policies must be unique by repository/base_branch")
            seen_keys.add(repository_policy.policy_key)
        return self

    @property
    def policy_sha256(self) -> str:
        return merge_train_policy_sha256(self)

    def find_repository_policy(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRepositoryPolicy:
        normalized_repository = _normalize_required_value(
            repository, "merge train policy lookup requires repository"
        )
        normalized_base_branch = _normalize_required_value(
            base_branch, "merge train policy lookup requires base_branch"
        )
        for repository_policy in self.policies:
            if (
                repository_policy.repository == normalized_repository
                and repository_policy.base_branch == normalized_base_branch
            ):
                return repository_policy
        raise ValueError(
            f"merge train policy not found for {normalized_repository}:{normalized_base_branch}"
        )


class MergeTrainPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    status: MergeTrainPolicyRecordStatus = "active"
    source: str
    updated_at: str
    policy_sha256: str = ""
    policy: MergeTrainPolicy

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainPolicyRecord":
        self.record_id = _normalize_required_value(
            self.record_id, "merge train policy record requires record_id"
        )
        self.source = _normalize_required_value(
            self.source, "merge train policy record requires source"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at, "merge train policy record requires updated_at"
        )
        normalize_merge_train_policy_timestamp(self.updated_at)
        computed_sha256 = self.policy.policy_sha256
        if not self.policy_sha256:
            self.policy_sha256 = computed_sha256
        if self.policy_sha256 != computed_sha256:
            raise ValueError(
                "merge train policy record policy_sha256 does not match policy payload"
            )
        return self


def build_merge_train_policy_record_id(*, updated_at: str, policy_sha256: str) -> str:
    normalized_timestamp = updated_at.replace("-", "").replace(":", "")
    normalized_timestamp = normalized_timestamp.replace("+00:00", "Z")
    return f"merge-train-policy-{normalized_timestamp}-{policy_sha256[:12]}"


def parse_merge_train_policy_toml(policy_toml: str) -> MergeTrainPolicy:
    return MergeTrainPolicy.model_validate(tomllib.loads(policy_toml))


def load_merge_train_policy(policy_file: Path) -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(policy_file.read_text(encoding="utf-8"))


def merge_train_policy_sha256(policy: MergeTrainPolicy) -> str:
    policy_payload = policy.model_dump(mode="json")
    for repository_policy in policy_payload.get("policies", ()):
        enqueue_policy = (
            repository_policy.get("enqueue") if isinstance(repository_policy, dict) else None
        )
        if isinstance(enqueue_policy, dict) and not enqueue_policy.get(
            "trusted_automation_github_user_ids"
        ):
            enqueue_policy.pop("trusted_automation_github_user_ids", None)
        if isinstance(repository_policy, dict) and not repository_policy.get(
            "stack_child_disposition_label"
        ):
            repository_policy.pop("stack_child_disposition_label", None)
        if (
            isinstance(repository_policy, dict)
            and repository_policy.get("engineering_review_mode") == "advisory"
        ):
            repository_policy.pop("engineering_review_mode", None)
        if isinstance(repository_policy, dict) and repository_policy.get("scheduler") == {
            "enabled": False,
            "runner_mode": "controller",
            "mutate": False,
        }:
            repository_policy.pop("scheduler", None)
        if isinstance(repository_policy, dict) and not repository_policy.get(
            "provider_delivery_protection_expectation"
        ):
            repository_policy.pop("provider_delivery_protection_expectation", None)
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def merge_train_policy_provider_expectation_projection(
    record: MergeTrainPolicyRecord | None,
) -> dict[str, dict[str, object]]:
    if record is None or record.status != "active":
        return {}
    projection: dict[str, dict[str, object]] = {}
    for repository_policy in sorted(record.policy.policies, key=lambda item: item.policy_key):
        expectation = repository_policy.provider_delivery_protection_expectation
        if expectation is not None:
            projection[repository_policy.policy_key] = cast(
                dict[str, object],
                expectation.model_dump(mode="json"),
            )
    return projection


def merge_train_repository_policy_delivery_semantics_sha256(
    policy: MergeTrainRepositoryPolicy,
) -> str:
    expectation = policy.provider_delivery_protection_expectation
    payload = {
        "repository": policy.repository,
        "base_branch": policy.base_branch,
        "enqueue_label": policy.enqueue_label,
        "blocked_label": policy.blocked_label,
        "stack_child_disposition_label": policy.stack_child_disposition_label,
        "merge_method": policy.merge_method,
        "failure_policy": policy.failure_policy,
        "engineering_review_mode": policy.engineering_review_mode,
        "enqueue": {
            "label_required": policy.enqueue.label_required,
            "allowed_actor_roles": sorted(policy.enqueue.allowed_actor_roles),
            "trusted_automation_github_user_ids": sorted(
                policy.enqueue.trusted_automation_github_user_ids
            ),
        },
        "provider_delivery_protection_expectation": (
            expectation.model_dump(mode="json") if expectation is not None else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{message}; value must be a single line")
    return normalized
