"""Resolve planning targets through their existing repository policy and authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import click

from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainRepositoryPolicy
from control_plane.workflows.launchplane import github_api_request


class PlanningScopeReview(click.ClickException):
    """The current delivery has no proven authority for this repository."""


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
        token = resolve_token(selected)
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
            token = resolve_token(target)
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
