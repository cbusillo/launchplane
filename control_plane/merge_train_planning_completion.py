"""GitHub adapter for planning metadata after a confirmed change landing.

This never closes an issue. Closing references only discover already-completed
records; waits, native prerequisites and reporter-owned records stay for review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import click

from control_plane.workflows.launchplane import github_api_request


_DEFAULT_LABELS = {
    "plan": "plan",
    "active": "plan:active",
    "blocked": "plan:blocked",
    "waiting": "plan:waiting",
    "stale": "plan:stale",
    "done": "plan:done",
}
_MARKER = "<!-- github-plan:wait "
_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String, $config: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    object(expression: $config) { ... on Blob { text } }
    pullRequest(number: $number) {
      merged baseRefName mergeCommit { oid }
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


class _IssueProjectReview(PlanningCompletionReview):
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
        raise PlanningCompletionReview("Planning completion GraphQL read or write was incomplete.")
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


def _wait_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    fence = ""
    html_end = ""
    for line in text.splitlines():
        if html_end:
            if html_end in line.casefold():
                html_end = ""
            continue
        html = re.match(
            r"^ {0,3}<(pre|code|blockquote|details|script|style|textarea)[\s>]", line, re.I
        )
        if html:
            end = f"</{html[1].casefold()}>"
            html_end = "" if end in line.casefold() else end
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opener:
            token = opener[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence or not line.startswith(_MARKER):
            continue
        match = re.fullmatch(re.escape(_MARKER) + r"(.*?) -->", line)
        if not match:
            raise PlanningCompletionReview(
                "Planning completion needs a valid structured wait review."
            )
        try:
            record = _object(json.loads(match[1]))
        except ValueError as error:
            raise PlanningCompletionReview(
                "Planning completion needs a valid structured wait review."
            ) from error
        if (
            set(record) != {"version", "state", "evidence", "waits"}
            or type(record["version"]) is not int
            or record["version"] != 1
            or record["state"] != "clear"
            or record["waits"] != []
            or not isinstance(record["evidence"], str)
            or not record["evidence"].strip()
        ):
            # A later clear must not hide a malformed or unresolved historical record.
            # Valid waiting records may be superseded; validate them before selection.
            if (
                set(record) != {"version", "state", "evidence", "waits"}
                or type(record.get("version")) is not int
                or record["version"] != 1
                or record.get("state") not in {"waiting", "review"}
                or not isinstance(record.get("waits"), list)
                or not isinstance(record.get("evidence"), str)
                or (
                    record["state"] == "waiting"
                    and (not record["waits"] or not record["evidence"].strip())
                )
                or (record["state"] == "review" and record["waits"])
            ):
                raise PlanningCompletionReview(
                    "Planning completion needs a valid structured wait review."
                )
            for wait in record["waits"]:
                if (
                    not isinstance(wait, dict)
                    or set(wait) != {"waiting_for", "ends_when"}
                    or any(
                        not isinstance(value, str) or not value.strip() for value in wait.values()
                    )
                ):
                    raise PlanningCompletionReview(
                        "Planning completion needs a valid structured wait review."
                    )
        records.append(record)
    if len(records) > 1:
        raise PlanningCompletionReview("Planning completion found ambiguous structured waits.")
    return records


def _require_clear_wait(
    issue: dict[str, Any], comments: list[dict[str, Any]], trusted: set[int], owner: str
) -> None:
    selected = None
    for source in [issue, *comments]:
        user = _object(source.get("user"))
        if (
            user.get("id") not in trusted
            and str(user.get("login", "")).casefold() != owner.casefold()
        ):
            continue
        found = _wait_records(str(source.get("body") or ""))
        if found:
            selected = found[0]
    if selected is None or selected["state"] != "clear":
        raise PlanningCompletionReview(
            "Planning completion preserves an unresolved or unreviewed wait."
        )


def _labels(issue: dict[str, Any]) -> set[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        raise PlanningCompletionReview("Planning completion issue labels were unavailable.")
    return {str(_object(label)["name"]) for label in labels}


def _require_completed(issue: dict[str, Any]) -> None:
    if issue.get("state") == "open" and "pull_request" not in issue:
        if issue.get("state_reason") == "reopened":
            raise PlanningCompletionReview(
                "Planning completion preserves a reopened finish line for owning review."
            )
        raise PlanningCompletionPending(
            "Planning completion is waiting for GitHub's keyword closure projection."
        )
    if (
        issue.get("state") != "closed"
        or issue.get("state_reason") != "completed"
        or "pull_request" in issue
    ):
        raise PlanningCompletionReview(
            "Planning completion preserves an open or non-completed finish line."
        )


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


def reconcile_completed_change(
    *,
    repository: str,
    base_branch: str,
    number: int,
    token: str,
    trusted_author_ids: tuple[int, ...],
) -> dict[str, Any]:
    """Return a receipt only after every discovered keyword-closed plan is read back."""
    owner, name = repository.split("/", 1)
    after = None
    references: list[int] = []
    config: dict[str, Any] = {}
    landing = None
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
        sha = _object(pull.get("mergeCommit") or {}).get("oid")
        if (
            pull.get("merged") is not True
            or pull.get("baseRefName") != base_branch
            or _object(repo.get("defaultBranchRef")).get("name") != base_branch
            or not sha
        ):
            raise PlanningCompletionReview(
                "Planning completion requires a confirmed default-branch landing."
            )
        if landing is not None and landing != sha:
            raise PlanningCompletionReview("Planning completion landing changed during its read.")
        landing = sha
        blob = repo.get("object")
        if blob is not None:
            config = _object(json.loads(_object(blob)["text"]))
        connection = _object(pull.get("closingIssuesReferences"))
        for issue in connection["nodes"]:
            if _object(issue["repository"])["nameWithOwner"].casefold() != repository.casefold():
                raise PlanningCompletionReview(
                    "Planning completion needs owning-agent review for a cross-repository closing reference."
                )
            references.append(int(issue["number"]))
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
    configured = _object(_object(config.get("planning", {})).get("labels", {}))
    labels = {**_DEFAULT_LABELS, **configured}
    stale = {labels[key] for key in ("active", "blocked", "waiting", "stale")}
    outcomes: list[dict[str, Any]] = []
    review_reasons: list[str] = []
    pending_reasons: list[str] = []
    for issue_number in dict.fromkeys(references):
        path = f"/repos/{repository}/issues/{issue_number}"
        issue = _object(github_api_request(path=path, token=token))
        names = _labels(issue)
        if not names.intersection(labels.values()):
            outcomes.append({"number": issue_number, "status": "not_planning_issue"})
            continue
        try:
            if _object(issue.get("user")).get(
                "id"
            ) not in trusted_author_ids or "<!-- github-plan:managed-provenance -->" not in str(
                issue.get("body") or ""
            ):
                raise PlanningCompletionReview(
                    f"Planning completion preserves reporter-owned issue #{issue_number}."
                )
            comments = _pages(token, path + "/comments")
            _require_clear_wait(issue, comments, set(trusted_author_ids), owner)
            _require_completed(issue)
            blockers = _pages(token, path + "/dependencies/blocked_by")
            children = _pages(token, path + "/sub_issues")
            if any(item.get("state") != "closed" for item in [*blockers, *children]):
                raise PlanningCompletionReview(
                    f"Planning completion preserves unfinished prerequisites for #{issue_number}."
                )
            fresh = _object(github_api_request(path=path, token=token))
            if fresh != issue or _pages(token, path + "/comments") != comments:
                raise PlanningCompletionReview(
                    "Planning completion issue changed before metadata reconciliation."
                )
        except PlanningCompletionPending as error:
            pending_reasons.append(f"#{issue_number}: {error.message}")
            outcomes.append({"number": issue_number, "status": "pending", "reason": error.message})
            continue
        except PlanningCompletionReview as error:
            review_reasons.append(f"#{issue_number}: {error.message}")
            outcomes.append({"number": issue_number, "status": "review", "reason": error.message})
            continue
        try:
            prepared_project = _prepare_project(
                token, issue, _object(config.get("planning", {})), owner
            )
        except _IssueProjectReview as error:
            review_reasons.append(f"#{issue_number}: {error.message}")
            outcomes.append({"number": issue_number, "status": "review", "reason": error.message})
            continue
        # A held source does not block other eligible sources in this PR. Provider
        # failures during metadata writes still stop delivery and use its existing
        # retry/denial handling; they are not treated as another source's hold.
        _sync_project(token, prepared_project)
        if labels["done"] not in names:
            github_api_request(
                path=path + "/labels", token=token, method="POST", body={"labels": [labels["done"]]}
            )
        for label in sorted(names & stale):
            github_api_request(
                path=path + "/labels/" + quote(label, safe=""), token=token, method="DELETE"
            )
        verified = _object(github_api_request(path=path, token=token))
        _require_completed(verified)
        if (
            _labels(verified) & stale
            or labels["done"] not in _labels(verified)
            or verified.get("body") != issue.get("body")
            or _pages(token, path + "/comments") != comments
        ):
            raise PlanningCompletionReview("Planning completion metadata readback did not match.")
        outcomes.append({"number": issue_number, "status": "reconciled"})
    incomplete = {"ok": False, "landing_sha": landing, "issues": outcomes}
    if pending_reasons:
        raise PlanningCompletionPending(
            "; ".join([*pending_reasons, *review_reasons]), receipt=incomplete
        )
    if review_reasons:
        raise PlanningCompletionReview("; ".join(review_reasons), receipt=incomplete)
    return {"ok": True, "landing_sha": landing, "issues": outcomes}
