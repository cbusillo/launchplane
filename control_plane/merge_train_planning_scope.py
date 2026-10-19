"""Resolve planning targets through their existing repository policy and authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import click

from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainRepositoryPolicy
from control_plane.merge_train_provider_failure import ProviderFailure, provider_failure
from control_plane.workflows.launchplane import github_api_request


class PlanningScopeReview(click.ClickException):
    """The current delivery has no proven authority for this repository."""

    def __init__(self, message: str, *, credential_failure: ProviderFailure | None = None) -> None:
        super().__init__(message)
        self.credential_failure = credential_failure


@dataclass(frozen=True)
class PlanningRepositoryScope:
    repository: str
    base_branch: str
    token: str = field(repr=False)
    trusted_author_ids: tuple[int, ...] = ()


type PlanningScopeResolver = Callable[[str], PlanningRepositoryScope]


def planning_scope_resolver(
    *,
    policy: MergeTrainPolicy,
    source_repository: str,
    authorize: Callable[[MergeTrainRepositoryPolicy], bool],
    resolve_token: Callable[[MergeTrainRepositoryPolicy], str],
) -> PlanningScopeResolver:
    """Select once before provider IO; a refusal never tries another credential."""
    owner = source_repository.split("/", 1)[0].casefold()

    def resolve_credential(target: MergeTrainRepositoryPolicy) -> str:
        try:
            return resolve_token(target)
        except click.ClickException as error:
            evidence = provider_failure(error)
            if evidence.retryable:
                raise
            # A failed target App mint proves no authority for that repository.
            # It says nothing about credentials already issued to other scopes.
            raise PlanningScopeReview(
                "Planning target's configured repository credential requires owning review.",
                credential_failure=evidence,
            ) from error

    def resolve(repository: str) -> PlanningRepositoryScope:
        if repository.split("/", 1)[0].casefold() != owner:
            raise PlanningScopeReview(
                "Planning target belongs to another account; owning review required."
            )
        eligible = [
            item
            for item in policy.policies
            if item.repository.casefold() == repository.casefold() and authorize(item)
        ]
        if not eligible:
            raise PlanningScopeReview("Planning target has no authorized repository policy.")
        selected = sorted(eligible, key=lambda item: item.policy_key)[0]
        token = resolve_credential(selected)
        if not token:
            raise PlanningScopeReview(
                "Planning target's configured repository credential is unavailable."
            )
        observed = github_api_request(path=f"/repos/{repository}", token=token)
        if (
            not isinstance(observed, dict)
            or not isinstance(observed.get("default_branch"), str)
            or not observed["default_branch"].strip()
        ):
            raise PlanningScopeReview("Planning target's actual default branch is unavailable.")
        try:
            target = policy.find_repository_policy(
                repository=repository, base_branch=observed["default_branch"]
            )
        except ValueError as error:
            raise PlanningScopeReview(
                "Planning target's actual default branch has no repository policy."
            ) from error
        if not authorize(target):
            raise PlanningScopeReview(
                "Planning target's actual default-branch policy is unauthorized."
            )
        if target.policy_key != selected.policy_key:
            token = resolve_credential(target)
            if not token:
                raise PlanningScopeReview(
                    "Planning target's default-branch credential is unavailable."
                )
        return PlanningRepositoryScope(
            target.repository,
            target.base_branch,
            token,
            target.enqueue.trusted_automation_github_user_ids,
        )

    return resolve
