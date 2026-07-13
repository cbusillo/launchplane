from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import click

from control_plane.child_process_errors import normalize_child_process_failure
from control_plane.contracts.work_graph_read_model import (
    WorkGraphPlanningIssueFacts,
    WorkItemFocus,
)


_API_VERSION_ARGS = ("-H", "X-GitHub-Api-Version: 2022-11-28")
GitHubCheckState = Literal["success", "pending", "failure", "unknown"]
_WORK_ITEM_FOCUS_BY_VALUE: dict[str, WorkItemFocus] = {
    "Now": "Now",
    "Next": "Next",
    "Waiting": "Waiting",
    "Later": "Later",
    "Done": "Done",
    "Unknown": "Unknown",
}


@dataclass(frozen=True)
class GitHubProjectPlanningFactsConfig:
    owner: str
    project_number: int
    limit: int = 200
    signal_limit: int = 50
    gh_binary: str = "gh"

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("GitHub Project planning facts require an owner")
        if self.project_number < 1:
            raise ValueError("GitHub Project planning facts require a positive project number")
        if self.limit < 1:
            raise ValueError("GitHub Project planning facts require a positive limit")
        if self.signal_limit < 1:
            raise ValueError("GitHub Project planning facts require a positive signal limit")
        if not self.gh_binary.strip():
            raise ValueError("GitHub Project planning facts require a gh binary")


def build_github_project_planning_facts(
    config: GitHubProjectPlanningFactsConfig,
) -> tuple[WorkGraphPlanningIssueFacts, ...]:
    facts = build_github_project_item_facts(config)
    return _enrich_planning_facts_with_github_signals(config=config, facts=facts)


def build_github_project_item_facts(
    config: GitHubProjectPlanningFactsConfig,
) -> tuple[WorkGraphPlanningIssueFacts, ...]:
    return _load_github_project_planning_facts(config)


def build_github_project_issue_keys(
    config: GitHubProjectPlanningFactsConfig,
) -> tuple[str, ...]:
    return tuple(
        f"{fact.repository}#{fact.number}"
        for fact in build_github_project_item_facts(config)
        if not fact.is_pull_request
    )


def _load_github_project_planning_facts(
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
    project_number = _int_env_value(
        environ,
        name="LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER",
    )
    limit = _int_env_value(
        environ,
        name="LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT",
        default=200,
    )
    signal_limit = _int_env_value(
        environ,
        name="LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT",
        default=50,
    )
    return GitHubProjectPlanningFactsConfig(
        owner=owner,
        project_number=project_number,
        limit=limit,
        signal_limit=signal_limit,
        gh_binary=environ.get("LAUNCHPLANE_WORK_GRAPH_GH_BINARY", "gh").strip() or "gh",
    )


def _int_env_value(environ: dict[str, str], *, name: str, default: int | None = None) -> int:
    value = environ.get(name, "").strip()
    if not value and default is not None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise click.ClickException(f"{name} must be a positive integer.") from error


def _run_gh_json(command: Sequence[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        failure = normalize_child_process_failure(
            operation="Load GitHub Project planning facts",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    except subprocess.CalledProcessError as error:
        failure = normalize_child_process_failure(
            operation="Load GitHub Project planning facts",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise click.ClickException("GitHub Project item-list returned invalid JSON.") from error
    data = _as_object(payload)
    if data is None:
        raise click.ClickException("GitHub Project item-list returned invalid JSON.")
    return data


def _enrich_planning_facts_with_github_signals(
    *, config: GitHubProjectPlanningFactsConfig, facts: tuple[WorkGraphPlanningIssueFacts, ...]
) -> tuple[WorkGraphPlanningIssueFacts, ...]:
    enriched: list[WorkGraphPlanningIssueFacts] = []
    for index, fact in enumerate(facts):
        if index >= config.signal_limit:
            enriched.append(fact)
            continue
        updates: dict[str, object] = {}
        blocked_by = _github_issue_relation_count(config=config, fact=fact, endpoint="blocked_by")
        blocking = _github_issue_relation_count(config=config, fact=fact, endpoint="blocking")
        subissues = _github_subissue_counts(config=config, fact=fact)
        if blocked_by is not None:
            updates["blocked_by"] = blocked_by
        if blocking is not None:
            updates["blocking"] = blocking
        if subissues is not None:
            updates["subissues_total"] = subissues[0]
            updates["subissues_completed"] = subissues[1]
        if fact.is_pull_request:
            check_state = _github_pr_check_state(config=config, fact=fact)
            if check_state is not None:
                updates["check_state"] = check_state
        enriched.append(
            fact
            if not updates
            else WorkGraphPlanningIssueFacts.model_validate(fact.model_dump() | updates)
        )
    return tuple(enriched)


def _github_issue_relation_count(
    *, config: GitHubProjectPlanningFactsConfig, fact: WorkGraphPlanningIssueFacts, endpoint: str
) -> int | None:
    payload = _run_optional_gh_json(
        (
            config.gh_binary,
            "api",
            *_API_VERSION_ARGS,
            f"repos/{fact.repository}/issues/{fact.number}/dependencies/{endpoint}",
        )
    )
    if payload is None:
        return None
    if not isinstance(payload, list):
        raise click.ClickException(
            f"GitHub issue {endpoint} endpoint did not return an array for "
            f"{fact.repository}#{fact.number}."
        )
    return len(payload)


def _github_subissue_counts(
    *, config: GitHubProjectPlanningFactsConfig, fact: WorkGraphPlanningIssueFacts
) -> tuple[int, int] | None:
    payload = _run_optional_gh_json(
        (
            config.gh_binary,
            "api",
            *_API_VERSION_ARGS,
            f"repos/{fact.repository}/issues/{fact.number}/sub_issues",
        )
    )
    if payload is None:
        return None
    if not isinstance(payload, list):
        raise click.ClickException(
            "GitHub sub_issues endpoint did not return an array for "
            f"{fact.repository}#{fact.number}."
        )
    completed = 0
    for raw_subissue in payload:
        subissue = _as_object(raw_subissue)
        if subissue is not None and _string(subissue.get("state")).lower() == "closed":
            completed += 1
    return len(payload), completed


def _github_pr_check_state(
    *, config: GitHubProjectPlanningFactsConfig, fact: WorkGraphPlanningIssueFacts
) -> GitHubCheckState | None:
    payload = _run_optional_gh_json(
        (
            config.gh_binary,
            "pr",
            "checks",
            str(fact.number),
            "--repo",
            fact.repository,
            "--json",
            "bucket,state,name,workflow,completedAt,startedAt",
        )
    )
    if payload is None:
        return None
    if not isinstance(payload, list):
        raise click.ClickException(
            f"GitHub PR checks did not return an array for {fact.repository}#{fact.number}."
        )
    buckets = {
        _string(check.get("bucket")).lower()
        for check in (_as_object(raw_check) for raw_check in payload)
        if check is not None
    }
    if not buckets:
        return "unknown"
    if buckets & {"fail", "cancel"}:
        return "failure"
    if buckets & {"pending"}:
        return "pending"
    if buckets <= {"pass", "skipping"}:
        return "success"
    return "unknown"


def _run_optional_gh_json(command: Sequence[str]) -> object | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        failure = normalize_child_process_failure(
            operation="Load GitHub Project planning signals",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    except subprocess.CalledProcessError as error:
        if error.returncode == 8 and tuple(command[1:3]) == ("pr", "checks"):
            return [{"bucket": "pending"}]
        failure = normalize_child_process_failure(
            operation="Load GitHub Project planning signals",
            tool="github_cli",
            exception=error,
        )
        if "not found" in failure.detail.lower() or "does not exist" in failure.detail.lower():
            return None
        raise click.ClickException(failure.operator_message()) from error
    try:
        payload: object = json.loads(result.stdout)
        return payload
    except json.JSONDecodeError as error:
        raise click.ClickException(
            "GitHub Project planning signals returned invalid JSON."
        ) from error


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
            "title": _string(content.get("title") or item.get("title")),
            "url": _string(content.get("url")),
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
    recognized_focus = _WORK_ITEM_FOCUS_BY_VALUE.get(focus)
    if recognized_focus is not None:
        return recognized_focus
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


github_labels = _labels
github_positive_int = _positive_int
github_string = _string
github_as_object = _as_object
