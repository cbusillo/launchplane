from __future__ import annotations

from collections.abc import Callable
import logging
import json
from typing import Any
from urllib.parse import quote

import click
from github_plan_status import wait_records

from control_plane.merge_train_planning_completion import (
    PlanningCompletionReview,
    _pages,
    _graphql,
    reconcile_issue_status,
)
from control_plane.merge_train_planning_scope import PlanningRepositoryScope, PlanningScopeReview
from control_plane.contracts.github_app_installation_token import GitHubAppInstallationToken
from control_plane.merge_train_provider_failure import provider_failure
from control_plane.workflows.launchplane import github_api_request

_LOGGER = logging.getLogger(__name__)


def confirm_planning_poster(
    issued: GitHubAppInstallationToken | None, trusted_ids: tuple[int, ...]
) -> None:
    if issued is None or not issued.app_slug or not trusted_ids:
        raise PlanningScopeReview("Planning poster identity is unverified or untrusted.")
    login = issued.app_slug + "[bot]"
    actor = github_api_request(path=f"/users/{quote(login, safe='')}", token=issued.token)
    if (
        not isinstance(actor, dict)
        or actor.get("type") != "Bot"
        or actor.get("login") != login
        or type(actor.get("id")) is not int
        or actor["id"] not in trusted_ids
    ):
        raise PlanningScopeReview("Planning poster is not an existing trusted wait author.")


def release_wait_candidates(scope: PlanningRepositoryScope, product: str) -> list[dict[str, Any]]:
    query = """query($query: String!, $after: String) {
      search(query: $query, type: ISSUE, first: 100, after: $after) {
        issueCount pageInfo { hasNextPage endCursor }
        nodes { __typename ... on Issue { number repository { nameWithOwner } } }
      }
    }"""
    terms = f"repo:{scope.repository} is:issue is:open in:body,comments {json.dumps(wait_records.release_event_hint(product))}"
    records: dict[int, dict[str, Any]] = {}
    total = None
    after = None
    for _ in range(10):
        result = _graphql(scope.token, query, {"query": terms, "after": after}).get("search")
        if (
            not isinstance(result, dict)
            or type(result.get("issueCount")) is not int
            or not isinstance(result.get("nodes"), list)
            or not isinstance(result.get("pageInfo"), dict)
        ):
            raise PlanningCompletionReview("Release wait candidate search is incomplete.")
        count = result["issueCount"]
        if count < 0 or count > 1000 or (total is not None and count != total):
            raise PlanningCompletionReview(
                "Release wait candidate coverage changed or exceeded its bound."
            )
        total = count
        for item in result["nodes"]:
            if (
                not isinstance(item, dict)
                or item.get("__typename") != "Issue"
                or type(item.get("number")) is not int
                or item["number"] < 1
                or not isinstance(item.get("repository"), dict)
                or not isinstance(item["repository"].get("nameWithOwner"), str)
                or item["repository"]["nameWithOwner"].casefold() != scope.repository.casefold()
            ):
                raise PlanningCompletionReview("Release wait candidate identity is incomplete.")
            records[item["number"]] = item
        page = result["pageInfo"]
        if page.get("hasNextPage") is False:
            if len(records) != total:
                raise PlanningCompletionReview("Release wait candidate coverage is incomplete.")
            return list(records.values())
        cursor = page.get("endCursor")
        if (
            page.get("hasNextPage") is not True
            or not isinstance(cursor, str)
            or not cursor
            or cursor == after
        ):
            raise PlanningCompletionReview("Release wait candidate pagination is incomplete.")
        after = cursor
    raise PlanningCompletionReview("Release wait candidate search exceeded its coverage bound.")


def released_change_contains(
    *,
    scope: PlanningRepositoryScope,
    required_commit: str,
    released_commit: str,
) -> bool:
    if required_commit == released_commit:
        return True
    try:
        comparison = github_api_request(
            path=f"/repos/{scope.repository}/compare/{required_commit}...{released_commit}",
            token=scope.token,
        )
    except click.ClickException as error:
        if provider_failure(error).status_code == 404:
            return False
        raise
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
    ensure_posting_author: Callable[[], None],
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    def trusted(source: dict[str, Any]) -> bool:
        return wait_records.author_is_trusted(
            source, scope.repository.split("/", 1)[0], author_ids=scope.trusted_author_ids
        )

    path = f"/repos/{scope.repository}/issues/{number}"
    issue = github_api_request(path=path, token=scope.token)
    if (
        not isinstance(issue, dict)
        or issue.get("number") != number
        or not isinstance(issue.get("repository_url"), str)
        or issue["repository_url"].casefold()
        != f"https://api.github.com/repos/{scope.repository}".casefold()
    ):
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
    human = "\n".join(
        [
            evidence,
            "If this is still the recorded release wait:",
            *("- " + " ".join(action.split()) for action in actions),
        ]
    )
    body = wait_records.marker(resolved) + "\n\n" + human.replace("@", "@\u200b")
    ensure_posting_author()
    if should_stop():
        return
    result = github_api_request(
        path=path + "/comments", token=scope.token, method="POST", body={"body": body}
    )
    if not isinstance(result, dict) or type(result.get("id")) is not int:
        raise ValueError("Release wait resolution was not confirmed.")
    observed = wait_records.current(issue, _pages(scope.token, path + "/comments"), trusted)
    if observed != wait_records.snapshot(resolved):
        raise ValueError("Release wait readback changed; preserve the newer record.")
    reconcile_issue_status(scope=scope, number=number)


def resolve_repository_release_waits(
    *,
    scope: PlanningRepositoryScope,
    product: str,
    evidence: str,
    contains_commit: Callable[[str], bool],
    release_still_current: Callable[[], bool],
    ensure_posting_author: Callable[[], None],
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    failures = 0
    if should_stop():
        return
    for issue in release_wait_candidates(scope, product):
        if should_stop():
            return
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
                ensure_posting_author=ensure_posting_author,
                should_stop=should_stop,
            )
        except (ValueError, click.ClickException) as error:
            if isinstance(error, click.ClickException) and not isinstance(
                error, PlanningCompletionReview
            ):
                if provider_failure(error).status_code not in {404, 410}:
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
