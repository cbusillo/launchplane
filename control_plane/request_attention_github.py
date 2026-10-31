from pathlib import Path
from typing import Any

from github_plan_status import next_actor, normalize_author
from github_plan_status.body import contributor_plan_body
from github_plan_status.discussion import (
    current_status,
    released_actor,
    released_claim_id,
    records,
    status_field,
)
from github_plan_status.waits import author_is_trusted, live_lines

from control_plane.github_payload import json_object
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.request_attention import CollectedRequest, OpenRequestPage, REQUEST_PAGE_LIMIT
from control_plane.workflows.launchplane import launchplane_github_token

_AUTHOR = "author { login __typename ... on User { databaseId } ... on Bot { databaseId } }"
_EDITOR = _AUTHOR.replace("author", "editor", 1)
_PAGE = "pageInfo { hasNextPage endCursor }"
_COMMENT = f"databaseId body createdAt updatedAt {_AUTHOR}"
_FIELDS = f"id url title body createdAt updatedAt lastEditedAt state {_AUTHOR} {_EDITOR} comments(first:100) {{ nodes {{ {_COMMENT} }} {_PAGE} }}"


class GitHubOpenRequestReader:
    def __init__(self, control_plane_root: Path) -> None:
        self.control_plane_root = control_plane_root

    def __call__(self, repository: str, *, trusted_author_ids: tuple[int, ...]) -> OpenRequestPage:
        with launchplane_github_token(
            control_plane_root=self.control_plane_root,
            context_name="launchplane",
            repository=repository,
        ) as token:
            if not token:
                raise ValueError("Source-control read credentials are unavailable.")
            transport = UrllibMergeTrainGitHubTransport(token=token)
            rows: list[CollectedRequest] = []
            truncated = False
            for connection, kind in (("issues", "request"), ("pullRequests", "change")):
                cursor = None
                seen: set[str] = set()
                for _ in range(REQUEST_PAGE_LIMIT):
                    extra = (
                        "closingIssuesReferences(first:100) { nodes { id } pageInfo { hasNextPage } }"
                        if kind == "change"
                        else ""
                    )
                    query = f"query($owner:String!,$name:String!,$cursor:String) {{ repository(owner:$owner,name:$name) {{ {connection}(first:100,after:$cursor,states:OPEN,orderBy:{{field:CREATED_AT,direction:ASC}}) {{ nodes {{ {_FIELDS} {extra} }} {_PAGE} }} }} }}"
                    owner, name = repository.split("/", 1)
                    data = _query(
                        transport, query, {"owner": owner, "name": name, "cursor": cursor}
                    )
                    page = _object(
                        _object(data.get("repository"), "repository").get(connection), "requests"
                    )
                    for raw in _nodes(page):
                        comments = _comments(transport, raw)
                        rows.append(
                            _collected_request(raw, comments, kind, owner, trusted_author_ids)
                        )
                    cursor = _next_cursor(page, seen)
                    if cursor is None:
                        break
                else:
                    truncated = True
            return OpenRequestPage(requests=tuple(rows), has_more=truncated)


def _query(
    transport: UrllibMergeTrainGitHubTransport, query: str, variables: dict[str, object]
) -> dict[str, Any]:
    payload = _object(
        transport.request(
            method="POST", path="/graphql", body={"query": query, "variables": variables}
        ),
        "response",
    )
    if payload.get("errors"):
        raise ValueError("Source-control query is incomplete.")
    return _object(payload.get("data"), "query data")


def _nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("Source-control connection omitted records.")
    return [_object(node, "source record") for node in nodes]


def _next_cursor(connection: dict[str, Any], seen: set[str]) -> str | None:
    info = _object(connection.get("pageInfo"), "page information")
    more = info.get("hasNextPage")
    if type(more) is not bool:
        raise ValueError("Source-control pagination coverage is unknown.")
    if not more:
        return None
    cursor = _text(info, "endCursor")
    if cursor in seen:
        raise ValueError("Source-control cursor did not advance.")
    seen.add(cursor)
    return cursor


def _comments(
    transport: UrllibMergeTrainGitHubTransport, raw: dict[str, Any]
) -> list[dict[str, Any]]:
    page = _object(raw.get("comments"), "discussion")
    comments = _nodes(page)
    seen: set[str] = set()
    for _ in range(REQUEST_PAGE_LIMIT):
        cursor = _next_cursor(page, seen)
        if cursor is None:
            return comments
        query = f"query($id:ID!,$cursor:String!) {{ node(id:$id) {{ ... on Issue {{ comments(first:100,after:$cursor) {{ nodes {{ {_COMMENT} }} {_PAGE} }} }} ... on PullRequest {{ comments(first:100,after:$cursor) {{ nodes {{ {_COMMENT} }} {_PAGE} }} }} }} }}"
        data = _query(transport, query, {"id": _text(raw, "id"), "cursor": cursor})
        page = _object(_object(data.get("node"), "source").get("comments"), "discussion")
        comments.extend(_nodes(page))
    raise ValueError("Source discussion exceeds the read bound.")


def _collected_request(
    raw: dict[str, Any],
    comments: list[dict[str, Any]],
    kind: str,
    owner: str,
    author_ids: tuple[int, ...],
) -> CollectedRequest:
    if raw.get("state") not in {"OPEN", "CLOSED", "MERGED"}:
        raise ValueError("Source omitted its open state.")
    trusted = {
        str(normalize_author(row)["login"]).casefold()
        for row in [raw, *comments]
        if author_is_trusted(row, owner, author_ids=author_ids)
    }
    discussion: dict[str, Any] = {
        "body": "",
        "updated_at": raw.get("updatedAt"),
        "body_recorded_at": raw.get("lastEditedAt") or raw.get("createdAt"),
        "comments": [
            {
                "id": row.get("databaseId"),
                "body": row.get("body") or "",
                "author": normalize_author(row)["login"],
                "created_at": row.get("createdAt"),
                "updated_at": row.get("updatedAt"),
            }
            for row in comments
        ],
    }
    action = "Triage the original request."
    try:
        if author_is_trusted(raw, owner, author_ids=author_ids):
            discussion["body"] = raw.get("body") or ""
        elif kind == "request" and author_is_trusted(
            {"author": raw.get("editor")}, owner, author_ids=author_ids
        ):
            discussion["body"] = contributor_plan_body(raw) or ""
        status = current_status(
            {
                **discussion,
                "body": _status_text(discussion["body"]),
                "comments": [
                    {**row, "body": _status_text(row["body"])} for row in discussion["comments"]
                ],
            },
            trusted,
            body_recorded_at=raw.get("lastEditedAt") or raw.get("createdAt"),
        )
        actor = next_actor.read(status)
        action = status_field(status, "Next action") or action
        if actor and actor.startswith("agent:"):
            authored = [
                {**row, "user": {"login": row["author"]}}
                for row in discussion["comments"]
                if str(row["author"]).casefold() in trusted
            ]
            receipts = [row for row in authored if "github-plan:abandoned-release " in row["body"]]
            claims = [
                row
                for row in authored
                if any(
                    record["session"] == actor.removeprefix("agent:")
                    for record in records(row["body"])
                )
            ]
            if claims and claims[-1]["id"] in {released_claim_id(row["body"]) for row in receipts}:
                actor, action = None, "Verify the recorded claim release in the source discussion."
            elif released_actor(
                actor,
                effective_comments=[row for row in authored if row not in receipts],
                trusted=trusted,
            ):
                actor = None
    except ValueError:
        actor, action = None, "Review the original request's ambiguous Current Status."
    canonical = _object(
        raw.get("closingIssuesReferences", {"nodes": [], "pageInfo": {"hasNextPage": False}}),
        "canonical requests",
    )
    if _object(canonical.get("pageInfo"), "canonical coverage").get("hasNextPage") is not False:
        raise ValueError("Canonical request coverage is incomplete.")
    return CollectedRequest.model_validate(
        {
            "source_id": _text(raw, "id"),
            "source_url": _text(raw, "url"),
            "source_kind": kind,
            "title": _text(raw, "title"),
            "requester": normalize_author(raw)["login"] or "Unknown requester",
            "next_actor": actor,
            "next_action": action,
            "created_at": _text(raw, "createdAt"),
            "open": raw["state"] == "OPEN",
            "canonical_source_ids": tuple(_text(row, "id") for row in _nodes(canonical)),
        }
    )


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Source omitted required text metadata.")
    return value


def _object(value: object, name: str) -> dict[str, Any]:
    return json_object(value, name, error_type=ValueError)


def _status_text(value: Any) -> str:
    return "\n".join(line for _, line in live_lines(str(value or ""), prefix=""))
