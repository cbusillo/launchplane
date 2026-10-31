from pathlib import Path
from urllib.parse import quote

from control_plane.github_payload import json_object
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.request_attention import CollectedRequest, OpenRequestPage
from control_plane.workflows.launchplane import launchplane_github_token


class GitHubOpenRequestReader:
    def __init__(self, control_plane_root: Path) -> None:
        self.control_plane_root = control_plane_root

    def __call__(self, repository: str, page: int) -> OpenRequestPage:
        with launchplane_github_token(
            control_plane_root=self.control_plane_root,
            context_name="launchplane",
            repository=repository,
            purpose="source_issue_feedback",
        ) as token:
            if not token:
                raise ValueError("Source-control read credentials are unavailable.")
            payload = UrllibMergeTrainGitHubTransport(token=token).request(
                method="GET",
                path=f"/repos/{quote(repository)}/issues?state=open&sort=created&direction=asc&per_page=100&page={page}",
            )
        if not isinstance(payload, list):
            raise ValueError("Source-control request list is unavailable.")
        return OpenRequestPage(
            requests=tuple(
                _collected_request(json_object(row, "request", error_type=ValueError))
                for row in payload
            ),
            has_more=len(payload) >= 100,
        )


def _collected_request(row: dict[str, object]) -> CollectedRequest:
    if row.get("state") not in {"open", "closed"}:
        raise ValueError("Request source omitted its open state.")
    user = row.get("user")
    return CollectedRequest.model_validate(
        {
            "source_id": _text(row, "node_id"),
            "source_url": _text(row, "html_url"),
            "source_kind": "change" if "pull_request" in row else "request",
            "title": _text(row, "title"),
            "requester": _text(user, "login") if isinstance(user, dict) else "Unknown requester",
            "body": row.get("body") or "",
            "created_at": _text(row, "created_at"),
            "open": row.get("state") == "open",
        }
    )


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Request source omitted required text metadata.")
    return value
