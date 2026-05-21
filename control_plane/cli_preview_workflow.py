from json import JSONDecodeError
import json
import os
from pathlib import Path
from typing import Literal, cast

import click
from pydantic import ValidationError

from control_plane.contracts.preview_workflow_contract import (
    PreviewWorkflowEvent,
    PreviewWorkflowEventName,
    PreviewWorkflowOperation,
    decide_preview_workflow_operation,
    preview_workflow_idempotency_key,
)


def register_preview_workflow_commands(work_graph: click.Group) -> None:
    work_graph.add_command(preview_workflow_decision, name="preview-workflow-decision")


@click.command("preview-workflow-decision")
@click.option(
    "--event-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="GitHub event payload JSON. Defaults to GITHUB_EVENT_PATH when set.",
)
@click.option(
    "--event-name",
    type=click.Choice(("pull_request", "pull_request_target", "workflow_dispatch")),
    help="GitHub event name. Defaults to GITHUB_EVENT_NAME when set.",
)
@click.option("--action", "event_action", default="", help="GitHub event action.")
@click.option(
    "--operation",
    type=click.Choice(("refresh", "destroy")),
    help="Manual workflow_dispatch preview operation.",
)
@click.option("--repository", default="", help="Current owner/name repository.")
@click.option("--anchor-repo", default="", help="Preview anchor owner/name repository.")
@click.option(
    "--anchor-pr-number",
    type=click.IntRange(min=1),
    default=None,
    help="Preview anchor pull request number.",
)
@click.option("--actor", default="", help="GitHub actor. Defaults to GITHUB_ACTOR.")
@click.option("--base-repository", default="", help="Pull request base owner/name repo.")
@click.option("--head-repository", default="", help="Pull request head owner/name repo.")
@click.option("--head-sha", default="", help="Pull request head SHA.")
@click.option(
    "--label",
    "label_names",
    multiple=True,
    help="Current pull request label. Repeat for each label.",
)
@click.option("--action-label", default="", help="Label added or removed by this event.")
@click.option("--preview-label", default="preview", show_default=True)
@click.option("--product", default="", help="Product key used for idempotency output.")
@click.option("--context", "context_name", default="", help="Preview context key.")
@click.option("--run-id", default="", help="GitHub run id. Defaults to GITHUB_RUN_ID.")
@click.option(
    "--run-attempt",
    default="",
    help="GitHub run attempt. Defaults to GITHUB_RUN_ATTEMPT.",
)
def preview_workflow_decision(
    event_file: Path | None,
    event_name: PreviewWorkflowEventName | str | None,
    event_action: str,
    operation: PreviewWorkflowOperation | str | None,
    repository: str,
    anchor_repo: str,
    anchor_pr_number: int | None,
    actor: str,
    base_repository: str,
    head_repository: str,
    head_sha: str,
    label_names: tuple[str, ...],
    action_label: str,
    preview_label: str,
    product: str,
    context_name: str,
    run_id: str,
    run_attempt: str,
) -> None:
    try:
        github_event = _load_preview_workflow_github_event(event_file)
        event = _build_preview_workflow_event(
            github_event=github_event,
            event_name=event_name,
            event_action=event_action,
            operation=operation,
            repository=repository,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            actor=actor,
            base_repository=base_repository,
            head_repository=head_repository,
            head_sha=head_sha,
            label_names=label_names,
            action_label=action_label,
            preview_label=preview_label,
        )
        decision = decide_preview_workflow_operation(event)
        idempotency_key = ""
        if decision.operation != "ignore":
            idempotency_key = preview_workflow_idempotency_key(
                product=product.strip() or event.repository,
                context=context_name.strip() or event.repository,
                operation=decision.operation,
                anchor_pr_number=event.anchor_pr_number,
                run_id=run_id.strip() or os.environ.get("GITHUB_RUN_ID", "").strip(),
                run_attempt=run_attempt.strip()
                or os.environ.get("GITHUB_RUN_ATTEMPT", "").strip()
                or "1",
            )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "event": event.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "idempotency_key": idempotency_key,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _load_preview_workflow_github_event(event_file: Path | None) -> dict[str, object]:
    resolved_event_file = event_file
    if resolved_event_file is None:
        event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
        resolved_event_file = Path(event_path) if event_path else None
    if resolved_event_file is None:
        return {}
    payload = json.loads(resolved_event_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("preview workflow event file must contain a JSON object")
    return payload


def _build_preview_workflow_event(
    *,
    github_event: dict[str, object],
    event_name: PreviewWorkflowEventName | str | None,
    event_action: str,
    operation: PreviewWorkflowOperation | str | None,
    repository: str,
    anchor_repo: str,
    anchor_pr_number: int | None,
    actor: str,
    base_repository: str,
    head_repository: str,
    head_sha: str,
    label_names: tuple[str, ...],
    action_label: str,
    preview_label: str,
) -> PreviewWorkflowEvent:
    pull_request = _preview_workflow_object(github_event.get("pull_request"))
    repository_payload = _preview_workflow_object(github_event.get("repository"))
    base_payload = _preview_workflow_object(pull_request.get("base"))
    head_payload = _preview_workflow_object(pull_request.get("head"))
    base_repo_payload = _preview_workflow_object(base_payload.get("repo"))
    head_repo_payload = _preview_workflow_object(head_payload.get("repo"))
    action_label_payload = _preview_workflow_object(github_event.get("label"))
    input_payload = _preview_workflow_object(github_event.get("inputs"))

    resolved_labels = label_names or _preview_workflow_label_names(
        pull_request.get("labels")
    )
    resolved_event_name = _preview_workflow_string(event_name) or os.environ.get(
        "GITHUB_EVENT_NAME", ""
    )
    resolved_action = event_action.strip() or _preview_workflow_string(
        github_event.get("action")
    )
    resolved_operation = _preview_workflow_string(operation) or _preview_workflow_string(
        input_payload.get("operation")
    )
    resolved_repository = repository.strip() or _preview_workflow_repository_name(
        repository_payload
    )
    resolved_anchor_repo = anchor_repo.strip() or _preview_workflow_repository_name(
        base_repo_payload
    )
    if not resolved_anchor_repo:
        resolved_anchor_repo = resolved_repository
    resolved_base_repository = (
        base_repository.strip()
        or _preview_workflow_repository_name(base_repo_payload)
        or resolved_anchor_repo
    )
    resolved_head_repository = (
        head_repository.strip()
        or _preview_workflow_repository_name(head_repo_payload)
        or resolved_base_repository
    )
    resolved_anchor_pr_number = anchor_pr_number or _preview_workflow_int(
        pull_request.get("number") or github_event.get("number") or input_payload.get("pr_number")
    )
    resolved_actor = actor.strip() or os.environ.get("GITHUB_ACTOR", "").strip()
    resolved_head_sha = head_sha.strip() or _preview_workflow_string(head_payload.get("sha"))
    resolved_action_label = action_label.strip() or _preview_workflow_string(
        action_label_payload.get("name")
    )
    return PreviewWorkflowEvent(
        event_name=cast(PreviewWorkflowEventName, resolved_event_name),
        action=resolved_action,
        operation=cast(Literal["refresh", "destroy", ""], resolved_operation),
        repository=resolved_repository,
        anchor_repo=resolved_anchor_repo,
        anchor_pr_number=resolved_anchor_pr_number,
        actor=resolved_actor,
        base_repository=resolved_base_repository,
        head_repository=resolved_head_repository,
        head_sha=resolved_head_sha,
        label_names=resolved_labels,
        action_label=resolved_action_label,
        preview_label=preview_label,
    )


def _preview_workflow_object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _preview_workflow_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _preview_workflow_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError("preview workflow event requires anchor_pr_number")


def _preview_workflow_repository_name(repository_payload: dict[str, object]) -> str:
    full_name = _preview_workflow_string(repository_payload.get("full_name"))
    if full_name:
        return full_name
    owner_payload = _preview_workflow_object(repository_payload.get("owner"))
    owner_login = _preview_workflow_string(owner_payload.get("login"))
    repository_name = _preview_workflow_string(repository_payload.get("name"))
    if owner_login and repository_name:
        return f"{owner_login}/{repository_name}"
    return ""


def _preview_workflow_label_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    labels: list[str] = []
    for item in value:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            label = _preview_workflow_string(item.get("name"))
        else:
            label = ""
        if label:
            labels.append(label)
    return tuple(labels)
