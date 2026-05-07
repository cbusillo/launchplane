from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
from typing import Callable, Sequence


EVERY_CODE_WEBHOOK_EVENTS = (
    "issues",
    "pull_request",
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
)
EVERY_CODE_WEBHOOK_URL = "https://launchplane.shinycomputers.com/v1/every-code/github-webhook"


@dataclass(frozen=True)
class EveryCodeLabelDefinition:
    name: str
    color: str
    description: str


EVERY_CODE_LABEL_DEFINITIONS = (
    EveryCodeLabelDefinition(
        name="every-code",
        color="FBCA04",
        description="Ask Every Code to work this issue.",
    ),
    EveryCodeLabelDefinition(
        name="preview",
        color="0E8A16",
        description="Launchplane preview automation is enabled for this PR.",
    ),
    EveryCodeLabelDefinition(
        name="preview-ready",
        color="1D76DB",
        description="Every Code preview is ready for source issue validation.",
    ),
    EveryCodeLabelDefinition(
        name="preview-approved",
        color="0E8A16",
        description="Every Code preview was approved by the source issue author.",
    ),
    EveryCodeLabelDefinition(
        name="preview-changes-requested",
        color="D93F0B",
        description="Every Code preview needs source issue author changes.",
    ),
    EveryCodeLabelDefinition(
        name="ready-to-merge",
        color="5319E7",
        description="Every Code PR is ready for repository owner merge review.",
    ),
)

Runner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class EveryCodeWebhookSyncResult:
    repo: str
    status: str
    hook_id: int = 0
    events: tuple[str, ...] = EVERY_CODE_WEBHOOK_EVENTS
    error: str = ""
    labels_synced: int = 0

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "repo": self.repo,
            "status": self.status,
            "events": list(self.events),
            "labels_synced": self.labels_synced,
        }
        if self.hook_id:
            payload["hook_id"] = self.hook_id
        if self.error:
            payload["error"] = self.error
        return payload


def sync_every_code_webhooks(
    *,
    owner: str,
    webhook_secret: str,
    webhook_url: str = EVERY_CODE_WEBHOOK_URL,
    topic: str = "every-code",
    events: tuple[str, ...] = EVERY_CODE_WEBHOOK_EVENTS,
    runner: Runner | None = None,
) -> tuple[EveryCodeWebhookSyncResult, ...]:
    normalized_owner = owner.strip()
    normalized_secret = webhook_secret.strip()
    if not normalized_owner:
        raise ValueError("Every Code webhook sync requires owner")
    if not normalized_secret:
        raise ValueError("Every Code webhook sync requires webhook_secret")
    run = runner or _run_gh
    repos = _topic_repositories(owner=normalized_owner, topic=topic, runner=run)
    results: list[EveryCodeWebhookSyncResult] = []
    for repo in repos:
        try:
            results.append(
                _sync_repo_webhook(
                    repo=repo,
                    webhook_url=webhook_url,
                    webhook_secret=normalized_secret,
                    events=events,
                    runner=run,
                )
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error)).strip()
            results.append(EveryCodeWebhookSyncResult(repo=repo, status="error", error=detail))
    return tuple(results)


def _topic_repositories(*, owner: str, topic: str, runner: Runner) -> tuple[str, ...]:
    query = (
        f".[] | select(.isArchived == false) | "
        f'select([(.repositoryTopics // [])[].name] | index("{topic}")) | .nameWithOwner'
    )
    result = runner(
        (
            "gh",
            "repo",
            "list",
            owner,
            "--limit",
            "1000",
            "--json",
            "nameWithOwner,isArchived,repositoryTopics",
            "--jq",
            query,
        ),
        None,
    )
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _sync_repo_webhook(
    *,
    repo: str,
    webhook_url: str,
    webhook_secret: str,
    events: tuple[str, ...],
    runner: Runner,
) -> EveryCodeWebhookSyncResult:
    hooks_payload = runner(("gh", "api", f"repos/{repo}/hooks"), None).stdout
    hooks = json.loads(hooks_payload)
    if not isinstance(hooks, list):
        raise ValueError(f"GitHub hooks response for {repo} must be a list")
    existing_hook = _find_webhook(hooks, webhook_url=webhook_url)
    hook_payload = json.dumps(
        {
            "name": "web",
            "active": True,
            "events": list(events),
            "config": {
                "url": webhook_url,
                "content_type": "json",
                "insecure_ssl": "0",
                "secret": webhook_secret,
            },
        }
    )
    if existing_hook is not None:
        hook_id_value = existing_hook.get("id")
        hook_id = int(hook_id_value) if isinstance(hook_id_value, int | str) else 0
        runner(
            ("gh", "api", "-X", "PATCH", f"repos/{repo}/hooks/{hook_id}", "--input", "-"),
            hook_payload,
        )
        labels_synced = _sync_repo_labels(repo=repo, runner=runner)
        return EveryCodeWebhookSyncResult(
            repo=repo,
            status="updated",
            hook_id=hook_id,
            labels_synced=labels_synced,
        )
    result = runner(
        ("gh", "api", "-X", "POST", f"repos/{repo}/hooks", "--input", "-"), hook_payload
    )
    created_hook = json.loads(result.stdout)
    hook_id = int(created_hook.get("id", 0)) if isinstance(created_hook, dict) else 0
    labels_synced = _sync_repo_labels(repo=repo, runner=runner)
    return EveryCodeWebhookSyncResult(
        repo=repo,
        status="created",
        hook_id=hook_id,
        labels_synced=labels_synced,
    )


def _sync_repo_labels(*, repo: str, runner: Runner) -> int:
    existing_labels_payload = runner(
        ("gh", "label", "list", "--repo", repo, "--json", "name"), None
    ).stdout
    existing_labels = json.loads(existing_labels_payload)
    if not isinstance(existing_labels, list):
        raise ValueError(f"GitHub labels response for {repo} must be a list")
    existing_names = {
        item.get("name")
        for item in existing_labels
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for label in EVERY_CODE_LABEL_DEFINITIONS:
        if label.name in existing_names:
            runner(
                (
                    "gh",
                    "label",
                    "edit",
                    label.name,
                    "--repo",
                    repo,
                    "--color",
                    label.color,
                    "--description",
                    label.description,
                ),
                None,
            )
        else:
            runner(
                (
                    "gh",
                    "label",
                    "create",
                    label.name,
                    "--repo",
                    repo,
                    "--color",
                    label.color,
                    "--description",
                    label.description,
                ),
                None,
            )
    return len(EVERY_CODE_LABEL_DEFINITIONS)


def _find_webhook(hooks: list[object], *, webhook_url: str) -> dict[str, object] | None:
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        config = hook.get("config")
        if isinstance(config, dict) and config.get("url") == webhook_url:
            return hook
    return None


def _run_gh(args: Sequence[str], input_text: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(args),
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
