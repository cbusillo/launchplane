from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import click

from control_plane.contracts.work_graph_read_model import (
    WorkGraphPlanningIssueFacts,
    WorkItemFocus,
)


@dataclass(frozen=True)
class GitHubProjectPlanningFactsConfig:
    owner: str
    project_number: int
    limit: int = 200
    gh_binary: str = "gh"

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("GitHub Project planning facts require an owner")
        if self.project_number < 1:
            raise ValueError("GitHub Project planning facts require a positive project number")
        if self.limit < 1:
            raise ValueError("GitHub Project planning facts require a positive limit")
        if not self.gh_binary.strip():
            raise ValueError("GitHub Project planning facts require a gh binary")


def build_github_project_planning_facts(
    config: GitHubProjectPlanningFactsConfig,
) -> tuple[WorkGraphPlanningIssueFacts, ...]:
    payload = _run_gh_json(
        (
            config.gh_binary,
            "project",
            "item-list",
            str(config.project_number),
            "--owner",
            config.owner,
            "--format",
            "json",
            "--limit",
            str(config.limit),
        )
    )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise click.ClickException("GitHub Project item-list did not return an items array")
    facts: list[WorkGraphPlanningIssueFacts] = []
    for raw_item in raw_items:
        item = _as_object(raw_item)
        if item is None:
            continue
        fact = _project_item_to_planning_facts(item)
        if fact is not None:
            facts.append(fact)
    return tuple(facts)


def load_github_project_planning_facts_config_from_env(
    environ: dict[str, str],
) -> GitHubProjectPlanningFactsConfig | None:
    owner = environ.get("LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER", "").strip()
    project_number_value = environ.get("LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER", "").strip()
    if not owner and not project_number_value:
        return None
    if not owner or not project_number_value:
        raise click.ClickException(
            "Set both LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER and "
            "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER to enable work graph Project facts."
        )
    try:
        project_number = int(project_number_value)
    except ValueError as error:
        raise click.ClickException(
            "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER must be a positive integer."
        ) from error
    limit_value = environ.get("LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT", "").strip()
    try:
        limit = int(limit_value) if limit_value else 200
    except ValueError as error:
        raise click.ClickException(
            "LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT must be a positive integer."
        ) from error
    return GitHubProjectPlanningFactsConfig(
        owner=owner,
        project_number=project_number,
        limit=limit,
        gh_binary=environ.get("LAUNCHPLANE_WORK_GRAPH_GH_BINARY", "gh").strip() or "gh",
    )


def _run_gh_json(command: Sequence[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "GitHub CLI is required for work graph Project facts."
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise click.ClickException(
            f"GitHub Project planning facts could not be loaded: {detail}"
        ) from error
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise click.ClickException("GitHub Project item-list returned invalid JSON.") from error
    data = _as_object(payload)
    if data is None:
        raise click.ClickException("GitHub Project item-list returned invalid JSON.")
    return data


def _project_item_to_planning_facts(item: dict[str, Any]) -> WorkGraphPlanningIssueFacts | None:
    content = _as_object(item.get("content"))
    if content is None:
        return None
    issue_type = _string(content.get("type")).lower()
    if issue_type not in {"issue", "pullrequest", "pull request"}:
        return None
    repository = _repository_from_project_item(item=item, content=content)
    number = _positive_int(content.get("number"))
    if not repository or number is None:
        return None
    status = _string(item.get("status"))
    focus = _focus_from_project_item(item=item, status=status)
    state = "closed" if status.lower() == "done" or focus == "Done" else None
    return WorkGraphPlanningIssueFacts.model_validate(
        {
            "repository": repository,
            "number": number,
            "state": state,
            "focus": focus,
            "manager": _string(item.get("manager")),
            "finish_line": _string(item.get("finish Line") or item.get("Finish Line")),
            "labels": tuple(_labels(item.get("labels"))),
            "updated_at": _string(content.get("updatedAt") or content.get("updated_at")),
            "is_pull_request": issue_type in {"pullrequest", "pull request"},
        }
    )


def _repository_from_project_item(*, item: dict[str, Any], content: dict[str, Any]) -> str:
    content_repository = _string(content.get("repository"))
    if "/" in content_repository and not content_repository.startswith("http"):
        return content_repository
    item_repository = _string(item.get("repository"))
    for candidate in (content_repository, item_repository):
        parsed = _repository_from_url(candidate)
        if parsed:
            return parsed
    content_url = _string(content.get("url"))
    return _repository_from_url(content_url)


def _repository_from_url(value: str) -> str:
    if not value.strip():
        return ""
    parsed = urlparse(value.strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}"


def _focus_from_project_item(*, item: dict[str, Any], status: str) -> WorkItemFocus | None:
    focus = _string(item.get("focus") or item.get("Focus"))
    if focus in {"Now", "Next", "Waiting", "Later", "Done", "Unknown"}:
        return cast(WorkItemFocus, focus)
    if status == "Done":
        return "Done"
    if status == "Waiting":
        return "Waiting"
    return None


def _labels(raw_labels: object) -> tuple[str, ...]:
    if not isinstance(raw_labels, list):
        return ()
    labels: list[str] = []
    for raw_label in raw_labels:
        if isinstance(raw_label, str) and raw_label.strip():
            labels.append(raw_label.strip())
            continue
        label = _as_object(raw_label)
        if label is None:
            continue
        name = _string(label.get("name"))
        if name:
            labels.append(name)
    return tuple(labels)


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.isdigit():
        number = int(value)
        if number >= 1:
            return number
    return None


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None
