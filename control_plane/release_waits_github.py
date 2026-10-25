from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any
from urllib.parse import quote

import click
from github_plan_status import wait_records

from control_plane.merge_train_planning_completion import (
    PlanningCompletionReview,
    _pages,
    reconcile_issue_status,
)
from control_plane.merge_train_planning_scope import PlanningRepositoryScope
from control_plane.merge_train_provider_failure import provider_failure
from control_plane.workflows.launchplane import github_api_request

_LOGGER = logging.getLogger(__name__)


def released_change_contains(
    *,
    scope: PlanningRepositoryScope,
    required_commit: str,
    released_commit: str,
) -> bool:
    if required_commit == released_commit:
        return True
    comparison = github_api_request(
        path=f"/repos/{scope.repository}/compare/{required_commit}...{released_commit}",
        token=scope.token,
    )
    if not isinstance(comparison, dict) or comparison.get("status") not in {
        "ahead",
        "behind",
        "identical",
        "diverged",
    }:
        raise ValueError("Released source ancestry is unavailable.")
    return comparison["status"] in {"ahead", "identical"}


def _resolve_issue_wait(
    *,
    scope: PlanningRepositoryScope,
    number: int,
    product: str,
    evidence: str,
    contains_commit: Callable[[str], bool],
    release_still_current: Callable[[], bool],
) -> None:
    def trusted(source: dict[str, Any]) -> bool:
        return wait_records.author_is_trusted(
            source, scope.repository.split("/", 1)[0], author_ids=scope.trusted_author_ids
        )

    path = f"/repos/{scope.repository}/issues/{number}"
    issue = github_api_request(path=path, token=scope.token)
    if not isinstance(issue, dict) or issue.get("number") != number:
        raise ValueError("Release wait issue identity is unavailable.")
    if issue.get("state") != "open" or "pull_request" in issue:
        return
    comments = _pages(scope.token, path + "/comments")
    record = wait_records.current(issue, comments, trusted)
    resolved, actions = wait_records.resolve_release(
        record, product=product, contains_commit=contains_commit, evidence=evidence
    )
    if not actions:
        if any(
            evidence in str(comment.get("body", "")) and trusted(comment) for comment in comments
        ):
            reconcile_issue_status(scope=scope, number=number)
        return
    fresh_issue = github_api_request(path=path, token=scope.token)
    fresh_comments = _pages(scope.token, path + "/comments")
    if fresh_issue != issue or fresh_comments != comments or not release_still_current():
        raise ValueError("Release wait changed before completion; preserve the newer record.")
    human = "\n".join([evidence, *("- " + " ".join(action.split()) for action in actions)])
    body = wait_records.marker(resolved) + "\n\n" + human.replace("@", "@\u200b")
    result = github_api_request(
        path=path + "/comments", token=scope.token, method="POST", body={"body": body}
    )
    if not isinstance(result, dict) or type(result.get("id")) is not int:
        raise ValueError("Release wait resolution was not confirmed.")
    observed = wait_records.current(issue, _pages(scope.token, path + "/comments"), trusted)
    if observed != resolved:
        raise ValueError("Release wait readback changed; preserve the newer record.")
    reconcile_issue_status(scope=scope, number=number)


def resolve_repository_release_waits(
    *,
    scope: PlanningRepositoryScope,
    product: str,
    evidence: str,
    contains_commit: Callable[[str], bool],
    release_still_current: Callable[[], bool],
    updated_since: str = "",
) -> None:
    failures = 0
    path = f"/repos/{scope.repository}/issues"
    if updated_since:
        path += f"?since={quote(updated_since, safe='')}"
    for issue in _pages(scope.token, path):
        if "pull_request" in issue:
            continue
        number = issue.get("number")
        if type(number) is not int or number < 1:
            raise ValueError("Release wait issue identity is unavailable.")
        try:
            _resolve_issue_wait(
                scope=scope,
                number=number,
                product=product,
                evidence=evidence,
                contains_commit=contains_commit,
                release_still_current=release_still_current,
            )
        except (ValueError, click.ClickException) as error:
            if isinstance(error, click.ClickException) and not isinstance(
                error, PlanningCompletionReview
            ):
                if provider_failure(error).status_code != 404:
                    raise
            failures += 1
            _LOGGER.warning(
                "release wait resolution unavailable repository=%s issue=%s error_type=%s",
                scope.repository,
                number,
                type(error).__name__,
            )
    if failures:
        raise ValueError(f"Release wait resolution incomplete for {failures} issues.")
