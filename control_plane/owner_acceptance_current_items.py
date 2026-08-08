from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    GitHubOpenPullRequest,
)
from control_plane.change_impact_service import (
    ChangeImpactPolicyReadStore,
    ChangeImpactRepositoryEvidenceProvider,
)
from control_plane.contracts.change_impact import (
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.owner_acceptance import (
    OwnerAcceptanceEvaluationUnavailableError,
    evaluate_owner_acceptance,
)


class OwnerAcceptanceCurrentItemsProvider(ChangeImpactRepositoryEvidenceProvider, Protocol):
    def list_open_pull_requests(
        self,
        repository: str,
        *,
        limit: int,
    ) -> tuple[GitHubOpenPullRequest, ...]: ...

    def resolve_current_item(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence: ...


@dataclass(frozen=True, slots=True)
class _BoundedCurrentItemEvidenceProvider:
    provider: OwnerAcceptanceCurrentItemsProvider

    def resolve(self, target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        return self.provider.resolve_current_item(target)


class OwnerAcceptanceCurrentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    pull_request_number: int = Field(ge=1)
    title: str
    url: str
    updated_at: str
    evaluation_status: Literal["available", "unavailable"]
    decision: OwnerAcceptanceDecision | None = None
    error_code: str = ""

    @model_validator(mode="after")
    def _validate_evaluation(self) -> "OwnerAcceptanceCurrentItem":
        if self.evaluation_status == "available":
            if self.decision is None or self.error_code:
                raise ValueError("Available Owner acceptance items require a decision only.")
        elif self.decision is not None or not self.error_code:
            raise ValueError("Unavailable Owner acceptance items require an error code only.")
        return self


class OwnerAcceptanceCurrentItemsRepositoryFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    error_code: Literal["open_pull_requests_unavailable"] = "open_pull_requests_unavailable"


class OwnerAcceptanceCurrentItemsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    derivation: Literal["active_change_impact_open_pull_requests"] = (
        "active_change_impact_open_pull_requests"
    )
    repository_count: int = Field(ge=0)
    repository_failure_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    evaluated_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    truncated: bool
    items: tuple[OwnerAcceptanceCurrentItem, ...]
    repository_failures: tuple[OwnerAcceptanceCurrentItemsRepositoryFailure, ...]


def build_owner_acceptance_current_items(
    *,
    store: ChangeImpactPolicyReadStore,
    repository_evidence_provider: OwnerAcceptanceCurrentItemsProvider,
    limit: int,
    max_repositories: int = 10,
) -> OwnerAcceptanceCurrentItemsResult:
    policies = store.list_change_impact_policy_records(status="active")
    repositories = tuple(dict.fromkeys(policy.repository for policy in policies))
    selected_repositories = repositories[:max_repositories]
    truncated = len(repositories) > len(selected_repositories)
    candidate_limit = min(limit + 1, 100)
    candidates: list[GitHubOpenPullRequest] = []
    repository_failures: list[OwnerAcceptanceCurrentItemsRepositoryFailure] = []

    for repository in selected_repositories:
        try:
            repository_candidates = repository_evidence_provider.list_open_pull_requests(
                repository,
                limit=candidate_limit,
            )
        except (ChangeImpactRepositoryEvidenceError, ValueError):
            repository_failures.append(
                OwnerAcceptanceCurrentItemsRepositoryFailure(repository=repository)
            )
            continue
        if len(repository_candidates) >= candidate_limit:
            truncated = True
        candidates.extend(repository_candidates[:candidate_limit])

    candidates.sort(key=lambda candidate: candidate.updated_at, reverse=True)
    if len(candidates) > limit:
        truncated = True
    selected_candidates = tuple(candidates[:limit])
    items: list[OwnerAcceptanceCurrentItem] = []
    for candidate in selected_candidates:
        try:
            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=_BoundedCurrentItemEvidenceProvider(
                    repository_evidence_provider
                ),
                target=ChangeImpactTargetReference(
                    repository=candidate.repository,
                    pull_request_number=candidate.pull_request_number,
                ),
            )
        except (OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError):
            items.append(
                OwnerAcceptanceCurrentItem(
                    repository=candidate.repository,
                    pull_request_number=candidate.pull_request_number,
                    title=candidate.title,
                    url=candidate.url,
                    updated_at=candidate.updated_at,
                    evaluation_status="unavailable",
                    error_code="owner_acceptance_evidence_unavailable",
                )
            )
            continue
        items.append(
            OwnerAcceptanceCurrentItem(
                repository=candidate.repository,
                pull_request_number=candidate.pull_request_number,
                title=candidate.title,
                url=candidate.url,
                updated_at=candidate.updated_at,
                evaluation_status="available",
                decision=decision,
            )
        )

    unavailable_count = sum(item.evaluation_status == "unavailable" for item in items)
    return OwnerAcceptanceCurrentItemsResult(
        repository_count=len(selected_repositories),
        repository_failure_count=len(repository_failures),
        candidate_count=len(selected_candidates),
        evaluated_count=len(items) - unavailable_count,
        unavailable_count=unavailable_count,
        truncated=truncated,
        items=tuple(items),
        repository_failures=tuple(repository_failures),
    )
