import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MergeTrainActorRole = Literal["repo_owner", "repo_admin"]
MergeTrainFailurePolicy = Literal["pause_train", "continue_after_blocking_pr"]
MergeTrainIdentityKind = Literal["github_actions_oidc", "github_app", "github_token_secret"]
MergeTrainMergeMethod = Literal["merge", "squash", "rebase"]


class MergeTrainEnqueuePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_required: bool = True
    allowed_actor_roles: tuple[MergeTrainActorRole, ...] = ("repo_owner", "repo_admin")

    @model_validator(mode="after")
    def _validate_enqueue_policy(self) -> "MergeTrainEnqueuePolicy":
        if not self.allowed_actor_roles:
            raise ValueError("merge train enqueue policy requires at least one actor role")
        self.allowed_actor_roles = tuple(dict.fromkeys(self.allowed_actor_roles))
        return self


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


class MergeTrainRepositoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    enqueue_label: str
    blocked_label: str
    merge_method: MergeTrainMergeMethod
    failure_policy: MergeTrainFailurePolicy
    enqueue: MergeTrainEnqueuePolicy
    merge_identity: MergeTrainIdentity
    service_authz: MergeTrainServiceAuthz = Field(default_factory=MergeTrainServiceAuthz)
    github_token: MergeTrainGitHubTokenSource = Field(default_factory=MergeTrainGitHubTokenSource)

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
        if self.enqueue_label == self.blocked_label:
            raise ValueError("merge train enqueue_label and blocked_label must differ")
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


SELLYOUROUTBOARD_MAIN_POLICY_TOML = """schema_version = 1

[[policies]]
repository = "cbusillo/sellyouroutboard"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
"""


def parse_merge_train_policy_toml(policy_toml: str) -> MergeTrainPolicy:
    return MergeTrainPolicy.model_validate(tomllib.loads(policy_toml))


def load_merge_train_policy(policy_file: Path) -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(policy_file.read_text(encoding="utf-8"))


def build_sellyouroutboard_main_merge_train_policy() -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(SELLYOUROUTBOARD_MAIN_POLICY_TOML)


def merge_train_policy_sha256(policy: MergeTrainPolicy) -> str:
    encoded = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
