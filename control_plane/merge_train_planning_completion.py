"""GitHub adapter for planning metadata after a confirmed change landing.

This never closes an issue. Closing references only discover already-completed
records; waits, native prerequisites and reporter-owned records stay for review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import click

from control_plane.workflows.launchplane import github_api_request


from github_plan_status import (
    DEFAULT_STATUS_LABELS,
    classify_outcome,
    derive,
    failure_disposition,
    is_planning_issue,
    label_changes,
    normalize_author,
    projection_outcome,
    receipt_summary,
    same_wait_projection,
    wait_records,
)
from github_plan_status.body import PLAN_MANAGED_PROVENANCE_MARKER, contributor_plan_body
from control_plane.merge_train_planning_scope import (
    PlanningRepositoryScope,
    PlanningScopeResolver,
    PlanningScopeReview,
)
from control_plane.merge_train_provider_failure import provider_failure

PROJECTION_GRACE = timedelta(minutes=15)
_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String, $config: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    object(expression: $config) { ... on Blob { text } }
    pullRequest(number: $number) {
      merged mergedAt baseRefName mergeCommit { oid }
      closingIssuesReferences(first: 100, after: $after) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class PlanningCompletionFailure(click.ClickException):
    """Incomplete completion evidence, optionally including successful sources."""

    def __init__(self, message: str, *, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class PlanningCompletionReview(PlanningCompletionFailure):
    """The saved completion requires an owning agent, not a provider retry."""


class PlanningCompletionPending(PlanningCompletionFailure):
    """GitHub has not yet projected the keyword closure after the merge."""


class PlanningCompletionSourceReview(PlanningCompletionReview):
    """All sources were visited; landed feedback may report their terminal holds."""


class PlanningCompletionProviderFailure(PlanningCompletionFailure):
    """An explicitly classified GraphQL failure, including HTTP-200 errors."""

    def __init__(
        self, message: str, *, retryable: bool, rate_limited: bool = False, cause: str | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.cause = cause


class _IssueReview(PlanningCompletionReview):
    """A deliberate hold on this source, rather than a provider failure."""


class _IssueProjectReview(_IssueReview):
    """Project membership evidence is missing for this source only."""


@dataclass(frozen=True)
class _ProjectCompletion:
    query: str
    read_variables: dict[str, Any]
    update_variables: dict[str, Any]


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanningCompletionReview(
            "Planning completion received an incomplete provider object."
        )
    return value


def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = _object(
        github_api_request(
            path="/graphql",
            token=token,
            method="POST",
            body={"query": query, "variables": variables},
        )
    )
    if response.get("errors"):
        errors = response["errors"]
        codes = set()
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict):
                    codes.add(str(error.get("type", "")).upper())
                    extensions = error.get("extensions")
                    if isinstance(extensions, dict):
                        codes.add(str(extensions.get("code", "")).upper())
        denied = bool(
            codes & {"FORBIDDEN", "UNAUTHORIZED", "UNAUTHENTICATED", "INSUFFICIENT_SCOPES"}
        )
        transient = (
            bool(
                codes
                & {
                    "RATE_LIMITED",
                    "SERVICE_UNAVAILABLE",
                    "TIMEOUT",
                    "INTERNAL",
                    "INTERNAL_SERVER_ERROR",
                }
            )
            and not denied
        )
        raise PlanningCompletionProviderFailure(
            "Planning completion GraphQL read or write was incomplete.",
            retryable=transient,
            rate_limited=transient and "RATE_LIMITED" in codes,
            cause="permission_denied"
            if denied
            else "rate_limited"
            if "RATE_LIMITED" in codes
            else "transient_provider_failure"
            if transient
            else None,
        )
    return _object(response.get("data"))


def _pages(token: str, path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 11):
        response = github_api_request(path=f"{path}?per_page=100&page={page}", token=token)
        if not isinstance(response, list):
            raise PlanningCompletionReview("Planning completion list read was incomplete.")
        rows.extend(_object(item) for item in response)
        if len(response) < 100:
            return rows
    raise PlanningCompletionReview("Planning completion list exceeded its coverage bound.")


def _labels(issue: dict[str, Any]) -> set[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise PlanningCompletionReview("Planning completion issue labels were unavailable.")
    return {str(_object(label)["name"]) for label in labels}


def _require_completed(issue: dict[str, Any]) -> None:
    if (
        issue.get("state") != "closed"
        or issue.get("state_reason") != "completed"
        or "pull_request" in issue
    ):
        raise PlanningCompletionReview(
            "Planning completion preserves an open or non-completed finish line."
        )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("planning completion requires an aware merge timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("planning completion requires an aware merge timestamp")
    return parsed.astimezone(timezone.utc)


def _require_projection(issue: dict[str, Any], merged_at: datetime, attempted_at: datetime) -> None:
    if issue.get("state") == "open" and "pull_request" not in issue:
        if attempted_at < merged_at + PROJECTION_GRACE:
            raise PlanningCompletionPending(
                "Planning completion is waiting for GitHub's keyword closure projection."
            )
        raise _IssueReview(
            "Planning completion preserves a still-open finish line after its projection grace; owning review required."
        )
    _require_completed(issue)


def _prepare_project(
    token: str, issue: dict[str, Any], config: dict[str, Any], owner: str
) -> _ProjectCompletion | None:
    project = _object(config.get("projects", {}))
    number = project.get("default_project")
    if not number:
        return None
    try:
        number = int(number)
    except (ValueError, TypeError) as error:
        raise PlanningCompletionReview(
            "Planning completion needs a numeric configured Project."
        ) from error
    project_owner = str(project.get("owner") or owner)
    owner_record = _object(
        github_api_request(path=f"/users/{quote(project_owner, safe='')}", token=token)
    )
    owner_kind = "organization" if owner_record.get("type") == "Organization" else "user"
    query = """
    query($owner: String!, $number: Int!, $issue: ID!) {
      OWNER_KIND(login: $owner) { projectV2(number: $number) { id fields(first: 100) {
        nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
        pageInfo { hasNextPage }
      } } }
      node(id: $issue) { ... on Issue { projectItems(first: 100) {
        nodes { id project { id } fieldValueByName(name: "Status") {
          ... on ProjectV2ItemFieldSingleSelectValue { optionId }
        } } pageInfo { hasNextPage }
      } } }
    }
    """
    query = query.replace("OWNER_KIND", owner_kind)
    data = _graphql(
        token, query, {"owner": project_owner, "number": number, "issue": issue["node_id"]}
    )
    projects = [
        entry.get("projectV2")
        for key in ("organization", "user")
        if isinstance(entry := data.get(key), dict) and entry.get("projectV2")
    ]
    if len(projects) != 1:
        raise PlanningCompletionReview(
            "Planning completion could not resolve the configured Project."
        )
    target = _object(projects[0])
    fields = _object(target.get("fields"))
    items = _object(_object(data.get("node")).get("projectItems"))
    if _object(fields.get("pageInfo")).get("hasNextPage") is not False:
        raise PlanningCompletionReview("Planning completion Project fields were truncated.")
    if _object(items.get("pageInfo")).get("hasNextPage") is not False:
        raise _IssueProjectReview("Planning completion issue Project items were truncated.")
    status = [field for field in fields["nodes"] if field.get("name") == "Status"]
    matches = [
        item for item in items["nodes"] if _object(item.get("project")).get("id") == target["id"]
    ]
    if len(status) != 1:
        raise PlanningCompletionReview("Planning completion needs the Project Status field.")
    done = [option for option in status[0]["options"] if option["name"] == "Done"]
    if len(done) != 1:
        raise PlanningCompletionReview("Planning completion Project lacks its Done option.")
    if len(matches) != 1:
        raise _IssueProjectReview("Planning completion needs the existing issue Project item.")
    if _object(matches[0].get("fieldValueByName") or {}).get("optionId") == done[0]["id"]:
        return None
    return _ProjectCompletion(
        query=query,
        read_variables={"owner": project_owner, "number": number, "issue": issue["node_id"]},
        update_variables={
            "project": target["id"],
            "item": matches[0]["id"],
            "field": status[0]["id"],
            "option": done[0]["id"],
        },
    )


def _sync_project(token: str, prepared: _ProjectCompletion | None) -> None:
    if prepared is None:
        return
    mutation = """
    mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
      updateProjectV2ItemFieldValue(input: { projectId: $project, itemId: $item,
        fieldId: $field, value: { singleSelectOptionId: $option } }) { projectV2Item { id } }
    }
    """
    _graphql(
        token,
        mutation,
        prepared.update_variables,
    )
    readback = _graphql(
        token,
        prepared.query,
        prepared.read_variables,
    )
    observed = _object(_object(readback.get("node")).get("projectItems"))
    if _object(observed.get("pageInfo")).get("hasNextPage") is not False or not any(
        item.get("id") == prepared.update_variables["item"]
        and _object(item.get("fieldValueByName") or {}).get("optionId")
        == prepared.update_variables["option"]
        for item in observed["nodes"]
    ):
        raise PlanningCompletionReview("Planning completion Project readback did not match.")


@dataclass
class _Target:
    repository: str
    number: int
    source: bool = False
    scope: PlanningRepositoryScope | None = field(default=None, repr=False)
    discovery_error: Exception | None = field(default=None, repr=False)


@dataclass
class _Snapshot:
    issue: dict[str, Any]
    config: dict[str, Any]
    labels: dict[str, str]
    relationships: dict[str, Any]
    wait: dict[str, Any]
    wait_failure: Exception | None = None


def _configuration(scope: PlanningRepositoryScope) -> dict[str, Any]:
    owner, name = scope.repository.split("/", 1)
    query = """query($owner: String!, $name: String!, $config: String!) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef { name } object(expression: $config) { ... on Blob { text } }
      }
    }"""
    repo = _object(
        _graphql(
            scope.token,
            query,
            {"owner": owner, "name": name, "config": f"{scope.base_branch}:.github/github.json"},
        ).get("repository")
    )
    if (
        _object(repo.get("defaultBranchRef")).get("name") != scope.base_branch
        or "object" not in repo
    ):
        raise PlanningCompletionReview(
            "Planning target default branch or configuration read changed."
        )
    blob = repo["object"]
    return {} if blob is None else _object(json.loads(_object(blob)["text"]))


def _label_names(config: dict[str, Any]) -> dict[str, str]:
    configured = _object(_object(config.get("planning", {})).get("labels", {}))
    names = {**DEFAULT_STATUS_LABELS, **configured}
    if any(not isinstance(value, str) or not value.strip() for value in names.values()) or len(
        {value.casefold() for value in names.values()}
    ) != len(names):
        raise PlanningCompletionReview("Planning target status-label configuration is invalid.")
    return names


def _reference(issue: dict[str, Any], default_repository: str) -> tuple[str, int]:
    number = issue.get("number")
    repository = issue.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("full_name", repository.get("nameWithOwner"))
    if "repository" in issue and not isinstance(repository, str):
        raise PlanningCompletionReview("Planning native relationship repository is incomplete.")
    if not isinstance(repository, str):
        url = issue.get("repository_url")
        repository = (
            url.removeprefix("https://api.github.com/repos/")
            if isinstance(url, str) and url.startswith("https://api.github.com/repos/")
            else default_repository
        )
    if (
        type(number) is not int
        or number <= 0
        or repository.count("/") != 1
        or any(not part for part in repository.split("/"))
    ):
        raise PlanningCompletionReview("Planning native relationship identity is incomplete.")
    return repository, number


def _parent(scope: PlanningRepositoryScope, number: int) -> dict[str, Any] | None:
    try:
        return _object(
            github_api_request(
                path=f"/repos/{scope.repository}/issues/{number}/parent", token=scope.token
            )
        )
    except click.ClickException as error:
        if provider_failure(error).status_code == 404:
            return None
        raise


def _snapshot(
    target: _Target,
    *,
    config: dict[str, Any] | None = None,
    initial_issue: dict[str, Any] | None = None,
) -> _Snapshot:
    assert target.scope is not None
    scope = target.scope
    path = f"/repos/{scope.repository}/issues/{target.number}"
    configuration = _configuration(scope) if config is None else config
    issue = (
        initial_issue
        if initial_issue is not None
        else _object(github_api_request(path=path, token=scope.token))
    )
    _labels(issue)
    names = _label_names(configuration)
    blockers = _pages(scope.token, path + "/dependencies/blocked_by")
    children = _pages(scope.token, path + "/sub_issues") if target.source else []
    relationships: dict[str, Any] = {"blocked_by": [], "sub_issues": []}
    for key, rows in (("blocked_by", blockers), ("sub_issues", children)):
        for item in rows:
            repository, number = _reference(item, scope.repository)
            if str(item.get("state", "")).casefold() not in {"open", "closed"}:
                raise PlanningCompletionReview("Planning native prerequisite state is incomplete.")
            relationships[key].append(
                {"repo": repository, "number": number, "state": item["state"].casefold()}
            )
    wait_failure = None
    try:
        comments = _pages(scope.token, path + "/comments")
        wait = wait_records.current(
            issue,
            comments,
            lambda source: wait_records.author_is_trusted(
                source, scope.repository.split("/", 1)[0], author_ids=scope.trusted_author_ids
            ),
        )
    except (click.ClickException, ValueError) as error:
        evidence = provider_failure(error)
        if issue.get("state") != "closed" or evidence.cause in {
            "permission_denied",
            "invalid_credentials",
        }:
            raise
        wait_failure = error
        wait = {
            "version": 1,
            "state": "review",
            "evidence": "Structured wait history is unread; preserve recorded waits.",
            "waits": [],
        }
    return _Snapshot(issue, configuration, names, relationships, wait, wait_failure)


def _same_snapshot(first: _Snapshot, second: _Snapshot, *, labels_changed: bool = False) -> bool:
    issue_matches = first.issue == second.issue
    if labels_changed:
        issue_matches = all(
            first.issue.get(key) == second.issue.get(key)
            for key in ("body", "state", "state_reason", "user", "title", "node_id")
        )
    return (
        issue_matches
        and first.config == second.config
        and first.relationships == second.relationships
        and same_wait_projection(first.issue, first.wait, second.wait)
    )


def _source_review(
    snapshot: _Snapshot, *, merged_at: datetime, attempted: datetime, scope: PlanningRepositoryScope
) -> str:
    issue = snapshot.issue
    # Contributor envelopes remain reporter-owned completion, including a valid
    # managed subsection. Label-only neighbors never edit either subsection.
    envelope = contributor_plan_body(issue)
    if (
        normalize_author(issue).get("id") not in scope.trusted_author_ids
        or envelope is not None
        or PLAN_MANAGED_PROVENANCE_MARKER not in str(issue.get("body") or "").splitlines()
    ):
        raise _IssueReview("Planning completion preserves reporter-owned source acceptance.")
    _require_projection(issue, merged_at, attempted)
    if snapshot.wait["state"] != "clear":
        return "Preserve the actual or unread wait; owning agent reviews source completion."
    if any(
        item["state"] != "closed"
        for item in [*snapshot.relationships["blocked_by"], *snapshot.relationships["sub_issues"]]
    ):
        raise _IssueReview("Planning completion preserves unfinished native prerequisites.")
    return ""


def _reconcile_target(
    target: _Target,
    row: dict[str, Any],
    *,
    merged_at: datetime,
    attempted: datetime,
    config: dict[str, Any] | None = None,
) -> None:
    assert target.scope is not None
    scope = target.scope
    initial_issue = None
    if target.source:
        initial_issue = _object(
            github_api_request(
                path=f"/repos/{scope.repository}/issues/{target.number}", token=scope.token
            )
        )
        if initial_issue.get("state") == "open":
            # Every keyword source can still be awaiting GitHub's closure,
            # including non-plan and reporter-owned records. Its shape or wait
            # history must not turn that temporary graph projection into a
            # terminal receipt that never refreshes its dependent again.
            _require_projection(initial_issue, merged_at, attempted)
    snapshot = _snapshot(target, config=config, initial_issue=initial_issue)
    if not is_planning_issue(snapshot.issue, snapshot.labels):
        row["status"] = "not_planning_issue"
        return
    review = (
        _source_review(snapshot, merged_at=merged_at, attempted=attempted, scope=scope)
        if target.source
        else ""
    )
    derived = derive(
        snapshot.issue, snapshot.relationships, snapshot.wait, label_names=snapshot.labels
    )
    row.update(
        derived_status=derived["status"], blocked_by=derived["blocked_by"], wait=snapshot.wait
    )
    changes = label_changes(sorted(_labels(snapshot.issue)), snapshot.labels, derived)
    project = None
    if target.source and not review:
        try:
            project = _prepare_project(
                scope.token,
                snapshot.issue,
                _object(snapshot.config.get("planning", {})),
                scope.repository.split("/", 1)[0],
            )
        except _IssueProjectReview as error:
            review = error.message
    fresh = _snapshot(target)
    if not _same_snapshot(snapshot, fresh):
        raise PlanningCompletionReview(
            "Planning issue, native graph, wait or configuration changed before reconciliation."
        )
    path = f"/repos/{scope.repository}/issues/{target.number}"
    if project is not None:
        row.update(writes_started=True, write_outcome="unknown")
        _sync_project(scope.token, project)
        row["write_outcome"] = "confirmed"
    if changes["add"]:
        row.update(writes_started=True, write_outcome="unknown")
        github_api_request(
            path=path + "/labels", token=scope.token, method="POST", body={"labels": changes["add"]}
        )
        row["write_outcome"] = "confirmed"
    for label in changes["remove"]:
        row.update(writes_started=True, write_outcome="unknown")
        github_api_request(
            path=path + "/labels/" + quote(label, safe=""), token=scope.token, method="DELETE"
        )
        row["write_outcome"] = "confirmed"
    verified = _snapshot(target)
    if not _same_snapshot(snapshot, verified, labels_changed=True) or {
        name.casefold() for name in _labels(verified.issue)
    } != {name.casefold() for name in changes["result"]}:
        raise PlanningCompletionReview("Planning metadata readback did not match.")
    row.update(
        projection_outcome(derived, "reconciled" if row.get("writes_started") else "unchanged")
    )
    if review:
        row.update(
            status="review_wait_record"
            if snapshot.wait["state"] != "clear"
            else "review_related_finish_line",
            verified=False,
            completion_review=review,
        )
    if snapshot.wait_failure is not None:
        row["wait_read_failure"] = (
            provider_failure(snapshot.wait_failure).cause or "incomplete_history"
        )
        if provider_failure(snapshot.wait_failure).retryable:
            raise snapshot.wait_failure


def reconcile_completed_change(
    *,
    repository: str,
    base_branch: str,
    number: int,
    token: str,
    trusted_author_ids: tuple[int, ...],
    attempted_at: str,
    scope_resolver: PlanningScopeResolver | None = None,
) -> dict[str, Any]:
    """Return a receipt only after every discovered keyword-closed plan is read back."""
    owner, name = repository.split("/", 1)
    after = None
    references: list[tuple[str, int]] = []
    config: dict[str, Any] = {}
    landing = None
    merged_at = None
    attempted = _timestamp(attempted_at)
    for _ in range(10):
        data = _graphql(
            token,
            _QUERY,
            {
                "owner": owner,
                "name": name,
                "number": number,
                "after": after,
                "config": f"{base_branch}:.github/github.json",
            },
        )
        repo = _object(data.get("repository"))
        pull = _object(repo.get("pullRequest"))
        default_branch = _object(repo.get("defaultBranchRef")).get("name")
        if (
            type(pull.get("merged")) is not bool
            or pull.get("baseRefName") != base_branch
            or not isinstance(default_branch, str)
            or not default_branch.strip()
        ):
            raise PlanningCompletionReview(
                "Planning completion requires confirmed pull-request and branch identities."
            )
        # Completed feedback also covers closed stack children and release-branch
        # trains. Their legacy comment is valid, but this is no keyword-closure
        # metadata proof. No issue discovery or mutation applies to these records.
        if pull["merged"] is False or default_branch != base_branch:
            if landing is not None:
                raise PlanningCompletionReview(
                    "Planning completion landing changed during its read."
                )
            return {
                "ok": False,
                "status": "not_applicable",
                "reason": "pull_request_not_merged"
                if pull["merged"] is False
                else "non_default_base",
                "issues": [],
            }
        sha = _object(pull.get("mergeCommit") or {}).get("oid")
        observed_merge_time = _timestamp(pull["mergedAt"])
        if not sha or observed_merge_time > attempted + PROJECTION_GRACE:
            raise PlanningCompletionReview(
                "Planning completion requires a confirmed default-branch landing and merge time."
            )
        if observed_merge_time > attempted:
            raise PlanningCompletionPending(
                "Planning completion is waiting for the worker clock to reach GitHub's merge time."
            )
        if landing is not None and (landing != sha or merged_at != observed_merge_time):
            raise PlanningCompletionReview("Planning completion landing changed during its read.")
        landing = sha
        merged_at = observed_merge_time
        blob = repo.get("object")
        if blob is not None:
            config = _object(json.loads(_object(blob)["text"]))
        connection = _object(pull.get("closingIssuesReferences"))
        for issue in connection["nodes"]:
            references.append(_reference(_object(issue), repository))
        if len(set(references)) > 100:
            raise PlanningCompletionReview(
                "Planning completion references exceeded their coverage bound."
            )
        page = _object(connection.get("pageInfo"))
        if page.get("hasNextPage") is False:
            break
        cursor = page.get("endCursor")
        if page.get("hasNextPage") is not True or not isinstance(cursor, str) or cursor == after:
            raise PlanningCompletionReview(
                "Planning completion closing-reference read was truncated."
            )
        after = cursor
    else:
        raise PlanningCompletionReview(
            "Planning completion references exceeded their coverage bound."
        )
    assert merged_at is not None
    source_scope = PlanningRepositoryScope(repository, base_branch, token, trusted_author_ids)
    scopes: dict[str, PlanningRepositoryScope] = {repository.casefold(): source_scope}
    targets: dict[tuple[str, int], _Target] = {}
    outcomes: list[dict[str, Any]] = []
    provider_errors: list[Exception] = []
    pending_reasons: list[str] = []
    denied_repositories: set[str] = set()
    stopped = False

    def resolve(target_repository: str) -> PlanningRepositoryScope:
        key = target_repository.casefold()
        if key not in scopes:
            if key.split("/", 1)[0] != owner.casefold() or scope_resolver is None:
                raise PlanningScopeReview(
                    "Planning target needs its own authorized repository scope."
                )
            scope = scope_resolver(target_repository)
            if (
                scope.repository.casefold() != key
                or not scope.base_branch.strip()
                or not scope.token
            ):
                raise PlanningScopeReview(
                    "Planning target repository scope is incomplete or mismatched."
                )
            scopes[key] = scope
        return scopes[key]

    def add(target_repository: str, target_number: int, *, source: bool = False) -> _Target:
        key = (target_repository.casefold(), target_number)
        if key not in targets:
            if len(targets) >= 100:
                raise PlanningCompletionReview(
                    "Planning direct neighborhood exceeded its coverage bound."
                )
            targets[key] = _Target(target_repository, target_number, source)
        if source:
            targets[key].source = True
        return targets[key]

    for target_repository, target_number in references:
        add(target_repository, target_number, source=True)
    # Discover the complete bounded one-hop neighborhood before any metadata write.
    # Non-plan closing sources still unblock planning dependents and native parents.
    for target in list(targets.values()):
        if stopped or target.repository.casefold() in denied_repositories:
            continue
        try:
            target.scope = resolve(target.repository)
            path = f"/repos/{target.scope.repository}/issues/{target.number}"
            dependents = _pages(target.scope.token, path + "/dependencies/blocking")
            for neighbor in dependents:
                neighbor_repository, neighbor_number = _reference(neighbor, target.repository)
                add(neighbor_repository, neighbor_number)
            parent = _parent(target.scope, target.number)
            if parent is not None:
                parent_repository, parent_number = _reference(parent, target.repository)
                add(parent_repository, parent_number)
        except (click.ClickException, KeyError, TypeError, ValueError) as error:
            target.discovery_error = error
            if isinstance(error, PlanningScopeReview):
                denied_repositories.add(target.repository.casefold())
                continue
            evidence = provider_failure(error)
            cause = (
                error.cause
                if isinstance(error, PlanningCompletionProviderFailure)
                else evidence.cause
            )
            disposition = failure_disposition(cause, None, writes_started=False)
            if disposition["repository_denied"]:
                denied_repositories.add(target.repository.casefold())
            stopped = stopped or bool(disposition["stop_writes"])

    for target in targets.values():
        row: dict[str, Any] = {
            "repo": target.repository,
            "number": target.number,
            "role": "source" if target.source else "related",
        }
        key = target.repository.casefold()
        if target.discovery_error is None and (stopped or key in denied_repositories):
            row.update(
                status="not_attempted_after_stop" if stopped else "not_attempted_posting_authority",
                reason="Prior provider outcome prevents writes in this delivery.",
            )
        else:
            try:
                if target.discovery_error is not None:
                    row["phase"] = "neighborhood_discovery"
                    raise target.discovery_error
                target.scope = resolve(target.repository)
                _reconcile_target(
                    target,
                    row,
                    merged_at=merged_at,
                    attempted=attempted,
                    config=config if key == repository.casefold() else None,
                )
            except PlanningCompletionPending as error:
                row.update(status="review_projection_pending", reason=error.message)
                pending_reasons.append(error.message)
            except (_IssueReview, PlanningScopeReview) as error:
                row.update(
                    status="review_reporter_acceptance"
                    if isinstance(error, _IssueReview)
                    else "review_posting_authority",
                    reason=error.message,
                )
                if isinstance(error, PlanningScopeReview):
                    denied_repositories.add(key)
                    if error.credential_failure is not None:
                        evidence = error.credential_failure
                        row.update(
                            phase="credential_resolution",
                            failure={
                                "cause": evidence.cause,
                                "status_code": evidence.status_code,
                                "request_id": evidence.request_id,
                                "write_outcome": None,
                                "retryable": False,
                            },
                        )
            except (click.ClickException, KeyError, TypeError, ValueError) as error:
                evidence = provider_failure(error)
                cause = (
                    error.cause
                    if isinstance(error, PlanningCompletionProviderFailure)
                    else evidence.cause
                )
                if row.get("write_outcome") == "unknown" and (
                    (evidence.status_code is not None and 400 <= evidence.status_code < 500)
                    or cause == "permission_denied"
                ):
                    row["write_outcome"] = "rejected"
                disposition = failure_disposition(
                    cause,
                    row.get("write_outcome"),
                    writes_started=bool(row.get("writes_started")),
                    validation_error=isinstance(
                        error, (KeyError, TypeError, ValueError, PlanningCompletionReview)
                    ),
                )
                row.update(
                    status=disposition["status"],
                    failure={
                        "cause": cause,
                        "status_code": evidence.status_code,
                        "request_id": evidence.request_id,
                        "write_outcome": row.get("write_outcome"),
                        "retryable": evidence.retryable,
                    },
                    reason=str(error),
                )
                if disposition["repository_denied"]:
                    denied_repositories.add(key)
                stopped = stopped or bool(disposition["stop_writes"])
                if (
                    target.source
                    or stopped
                    or evidence.retryable
                    or (isinstance(error, PlanningCompletionProviderFailure) and error.retryable)
                ):
                    if disposition["stop_writes"]:
                        provider_errors.insert(0, error)
                    else:
                        provider_errors.append(error)
        row.update(
            classify_outcome(
                row["status"],
                verified=bool(row.get("verified")),
                required=target.source,
                projection_status=row.get("projection_status"),
                completion_required=target.source,
            )
        )
        outcomes.append(row)
    receipt = {**receipt_summary(outcomes), "landing_sha": landing, "issues": outcomes}
    if provider_errors:
        first_error = provider_errors[0]
        if isinstance(first_error, PlanningCompletionFailure):
            first_error.receipt = receipt
            raise first_error
        failure = PlanningCompletionFailure(
            "Planning reconciliation has incomplete provider evidence.", receipt=receipt
        )
        raise failure from first_error
    if pending_reasons:
        raise PlanningCompletionPending("; ".join(pending_reasons), receipt=receipt)
    if not receipt["ok"]:
        raise PlanningCompletionSourceReview(
            "Planning source completion requires owning-agent review.", receipt=receipt
        )
    return receipt
