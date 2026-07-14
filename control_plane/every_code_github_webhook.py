from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
from typing import Iterable, Protocol, cast

import click
from pydantic import BaseModel

from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackKind,
    EveryCodePrFeedbackRecord,
    build_every_code_pr_feedback_id,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    close_every_code_work_request_for_issue,
    close_every_code_work_request_for_pull_request,
)
from control_plane.every_code_work_request_write import (
    EveryCodeWorkRequestCreateEnvelope,
    build_every_code_work_request_record,
)
from control_plane.workflows.launchplane import (
    ProductProfileListStore,
    launchplane_anchor_repo_context,
    resolve_launchplane_github_token,
    verify_github_webhook_signature,
)
from control_plane.workflows.preview_pr_feedback import (
    handle_every_code_preview_validation_comment,
)

_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET"
_EVERY_CODE_TRIGGER_LABEL = "every-code"
_GITHUB_CLOSING_REFERENCE_PATTERN = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+([^\n\r]+)", re.IGNORECASE
)
_GITHUB_ISSUE_REFERENCE_PATTERN = re.compile(
    r"https://github\.com/(?P<url_repository>[^/\s]+/[^/\s]+)/issues/(?P<url_number>\d+)"
    r"|(?:(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#|#)(?P<number>\d+)",
    re.IGNORECASE,
)

_EveryCodeWebhookResponse = tuple[int, dict[str, object]]


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _WebhookSecretProvider(Protocol):
    def __call__(self) -> str: ...


class _GitHubWebhookSignatureVerifier(Protocol):
    def __call__(
        self,
        *,
        payload_bytes: bytes,
        signature_header: str,
        secret: str,
    ) -> None: ...


class _LaunchplaneAnchorRepoContextResolver(Protocol):
    def __call__(self, *, record_store: ProductProfileListStore, repo: str) -> str: ...


class _LaunchplaneGitHubTokenResolver(Protocol):
    def __call__(self, *, control_plane_root: Path, context_name: str) -> str: ...


def _webhook_secret_from_env() -> str:
    return os.environ.get(_EVERY_CODE_GITHUB_WEBHOOK_SECRET_ENV_KEY, "").strip()


@dataclass(frozen=True)
class EveryCodeGitHubWebhookDependencies:
    webhook_secret: _WebhookSecretProvider = _webhook_secret_from_env
    verify_signature: _GitHubWebhookSignatureVerifier = verify_github_webhook_signature
    now_timestamp: Callable[[], str] = _utc_now_timestamp
    anchor_repo_context: _LaunchplaneAnchorRepoContextResolver = launchplane_anchor_repo_context
    github_token: _LaunchplaneGitHubTokenResolver = resolve_launchplane_github_token


EveryCodeGitHubWebhookHandler = Callable[
    [bytes, str, str, str, object, Path, str], _EveryCodeWebhookResponse
]


class _EveryCodeWorkRequestStore(Protocol):
    def write_every_code_work_request_record(
        self, record: EveryCodeWorkRequestRecord
    ) -> object: ...

    def create_every_code_work_request_record_if_absent(
        self, record: EveryCodeWorkRequestRecord
    ) -> tuple[EveryCodeWorkRequestRecord, bool]: ...

    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None: ...

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object: ...

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]: ...

    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object: ...

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


def _every_code_work_request_store(record_store: object) -> _EveryCodeWorkRequestStore:
    required_methods = (
        "write_every_code_work_request_record",
        "create_every_code_work_request_record_if_absent",
        "read_every_code_work_request_record",
        "list_every_code_work_request_records",
        "claim_every_code_work_request_record",
        "write_every_code_pr_feedback_record",
        "list_every_code_pr_feedback_records",
        "write_every_code_preview_gate_record",
        "list_every_code_preview_gate_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_EveryCodeWorkRequestStore, record_store)
    raise TypeError("record store does not support Every Code work requests")


def _github_webhook_mapping(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _github_webhook_string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _github_webhook_raw_string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _github_webhook_positive_int(mapping: dict[str, object] | None, key: str) -> int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if type(value) is int and value >= 1:
        return value
    return None


def _github_repository_full_name_is_valid(repository: str) -> bool:
    if repository.strip() != repository:
        return False
    owner, separator, name = repository.partition("/")
    if not (owner and separator and name) or "/" in name:
        return False
    return all(_github_repository_component_is_valid(part) for part in (owner, name))


def _github_repository_component_is_valid(value: str) -> bool:
    return bool(
        value
        and value.strip() == value
        and all(character.isalnum() or character in {".", "_", "-"} for character in value)
    )


def _github_login_normalized(login: str) -> str:
    return login.strip().lstrip("@").casefold()


def _github_actor_login(payload: dict[str, object]) -> str:
    return _github_webhook_string(_github_webhook_mapping(payload, "sender"), "login")


def _every_code_trusted_manager_logins(repository: str) -> frozenset[str]:
    normalized_repository = repository.strip().casefold()
    if not normalized_repository:
        return frozenset()
    config_paths = (
        Path.home() / ".code" / "github-planning.json",
        Path.home() / ".codex" / "github-planning.json",
    )
    managers: set[str] = set()
    for config_path in config_paths:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict):
            continue
        default_manager = workflow.get("default_manager")
        if isinstance(default_manager, str) and default_manager.strip():
            managers.add(_github_login_normalized(default_manager))
        repo_managers = workflow.get("repo_managers")
        if isinstance(repo_managers, dict):
            repo_manager = repo_managers.get(repository) or repo_managers.get(normalized_repository)
            if isinstance(repo_manager, str) and repo_manager.strip():
                managers.add(_github_login_normalized(repo_manager))
    return frozenset(manager for manager in managers if manager)


def _every_code_feedback_actor_is_trusted(
    *,
    repository: str,
    actor: str,
    source_issue_author: str = "",
) -> bool:
    normalized_actor = _github_login_normalized(actor)
    if not normalized_actor:
        return False
    repository_owner = repository.strip().split("/", 1)[0]
    trusted = {_github_login_normalized(repository_owner)}
    if source_issue_author.strip():
        trusted.add(_github_login_normalized(source_issue_author))
    trusted.update(_every_code_trusted_manager_logins(repository))
    return normalized_actor in trusted


def _every_code_untrusted_feedback_response(
    *,
    trace_id: str,
    delivery_id: str,
) -> _EveryCodeWebhookResponse:
    return (
        202,
        {
            "status": "accepted",
            "trace_id": trace_id,
            "skipped": True,
            "reason": "untrusted_actor",
            "github_delivery_id": delivery_id,
        },
    )


def _every_code_github_webhook_invalid_payload_response(
    trace_id: str,
) -> _EveryCodeWebhookResponse:
    return (
        400,
        {
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "invalid_request",
                "message": "GitHub webhook payload is invalid.",
            },
        },
    )


def handle_every_code_github_webhook_request(
    body_bytes: bytes,
    event_name: str,
    delivery_id: str,
    signature_header: str,
    record_store: object,
    control_plane_root_path: Path,
    trace_id: str,
    *,
    dependencies: EveryCodeGitHubWebhookDependencies | None = None,
) -> _EveryCodeWebhookResponse:
    resolved_dependencies = dependencies or EveryCodeGitHubWebhookDependencies()
    secret = resolved_dependencies.webhook_secret()
    if not secret:
        return (
            503,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_secret_not_configured",
                    "message": "Every Code GitHub webhook secret is not configured.",
                },
            },
        )

    try:
        resolved_dependencies.verify_signature(
            payload_bytes=body_bytes,
            signature_header=signature_header,
            secret=secret,
        )
    except click.ClickException:
        return (
            401,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "webhook_signature_invalid",
                    "message": "GitHub webhook signature verification failed.",
                },
            },
        )

    if not delivery_id.strip():
        return (
            400,
            {
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "github_delivery_required",
                    "message": "GitHub webhook delivery id is required.",
                },
            },
        )

    normalized_delivery_id = delivery_id.strip()
    normalized_event_name = event_name.strip()
    payload = _decode_json_request_body_or_none(body_bytes)
    if payload is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    return _handle_decoded_every_code_github_webhook_request(
        trace_id=trace_id,
        normalized_delivery_id=normalized_delivery_id,
        normalized_event_name=normalized_event_name,
        payload=payload,
        record_store=record_store,
        control_plane_root_path=control_plane_root_path,
        dependencies=resolved_dependencies,
    )


def build_every_code_github_webhook_handler(
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> EveryCodeGitHubWebhookHandler:
    def handler(
        body_bytes: bytes,
        event_name: str,
        delivery_id: str,
        signature_header: str,
        record_store: object,
        control_plane_root_path: Path,
        trace_id: str,
    ) -> _EveryCodeWebhookResponse:
        return handle_every_code_github_webhook_request(
            body_bytes,
            event_name,
            delivery_id,
            signature_header,
            record_store,
            control_plane_root_path,
            trace_id,
            dependencies=dependencies,
        )

    return handler


def _handle_decoded_every_code_github_webhook_request(
    *,
    trace_id: str,
    normalized_delivery_id: str,
    normalized_event_name: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root_path: Path,
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> _EveryCodeWebhookResponse:
    if normalized_event_name == "issue_comment":
        preview_validation_response = _handle_every_code_preview_validation_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
            dependencies=dependencies,
        )
        if preview_validation_response is not None:
            return preview_validation_response
    if normalized_event_name in {
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
    }:
        return _handle_every_code_pr_feedback_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            event_name=normalized_event_name,
            payload=payload,
            record_store=record_store,
            dependencies=dependencies,
        )
    if normalized_event_name == "pull_request":
        return _handle_every_code_pull_request_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
            dependencies=dependencies,
        )
    if normalized_event_name != "issues":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_event",
            },
        )
    if payload.get("action") == "closed":
        return _handle_every_code_issue_closed_webhook(
            trace_id=trace_id,
            delivery_id=normalized_delivery_id,
            payload=payload,
            record_store=record_store,
            dependencies=dependencies,
        )
    if payload.get("action") != "labeled":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    label = _github_webhook_mapping(payload, "label")
    label_name = _github_webhook_string(label, "name")
    if label_name.strip().lower() != _EVERY_CODE_TRIGGER_LABEL:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "label_not_matched",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    sender_payload = _github_webhook_mapping(payload, "sender")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    issue_url = _github_webhook_string(issue_payload, "html_url")
    issue_number_value = _github_webhook_positive_int(issue_payload, "number")
    if (
        issue_number_value is None
        or not _github_repository_full_name_is_valid(repository)
        or not issue_url.strip()
    ):
        return _every_code_github_webhook_invalid_payload_response(trace_id)

    request = EveryCodeWorkRequestCreateEnvelope(
        repository=repository,
        issue_number=issue_number_value,
        issue_url=issue_url,
        issue_title=_github_webhook_string(issue_payload, "title"),
        trigger_label=_EVERY_CODE_TRIGGER_LABEL,
        trigger_actor=_github_webhook_string(sender_payload, "login"),
        github_delivery_id=normalized_delivery_id,
        source="github_issue_label",
        queued_at=dependencies.now_timestamp(),
    )
    record = build_every_code_work_request_record(request, queued_at=request.queued_at)
    every_code_store = _every_code_work_request_store(record_store)
    stored_record, created = every_code_store.create_every_code_work_request_record_if_absent(
        record
    )
    deduped = not created

    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={"request_id": stored_record.request_id, "state": stored_record.state},
        driver_result={"request": stored_record.model_dump(mode="json")},
    )
    accepted_payload["deduped"] = deduped
    accepted_payload["github_delivery_id"] = normalized_delivery_id
    return 202, accepted_payload


def _handle_every_code_issue_closed_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> _EveryCodeWebhookResponse:
    repository_payload = _github_webhook_mapping(payload, "repository")
    issue_payload = _github_webhook_mapping(payload, "issue")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    issue_number_value = _github_webhook_positive_int(issue_payload, "number")
    if issue_number_value is None or not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    issue_url = _github_webhook_string(issue_payload, "html_url")
    closed_at = _github_webhook_string(issue_payload, "closed_at") or dependencies.now_timestamp()
    state_reason = _github_webhook_string(issue_payload, "state_reason")

    every_code_store = _every_code_work_request_store(record_store)
    updated_records: list[EveryCodeWorkRequestRecord] = []
    terminal_records: list[EveryCodeWorkRequestRecord] = []
    for record in _iter_every_code_work_request_records(
        every_code_store,
        repository=repository,
    ):
        if record.issue_number != issue_number_value:
            continue
        if issue_url.strip() and record.issue_url.strip() != issue_url.strip():
            continue
        closed_record = close_every_code_work_request_for_issue(
            record,
            closed_at=closed_at,
            reason=state_reason,
        )
        if closed_record is None:
            terminal_records.append(record)
            continue
        every_code_store.write_every_code_work_request_record(closed_record)
        updated_records.append(closed_record)

    if not updated_records:
        response_payload: dict[str, object] = {
            "status": "accepted",
            "trace_id": trace_id,
            "skipped": True,
            "reason": "linked_every_code_request_not_found",
            "github_delivery_id": delivery_id,
        }
        if terminal_records:
            terminal_record = terminal_records[0]
            response_payload["reason"] = "linked_every_code_request_already_terminal"
            response_payload["result"] = {
                "request_id": terminal_record.request_id,
                "state": terminal_record.state,
            }
        return 202, response_payload

    updated_record = updated_records[0]
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "request_id": updated_record.request_id,
            "state": updated_record.state,
            "closed_count": len(updated_records),
        },
        driver_result={
            "request": updated_record.model_dump(mode="json"),
            "closed_count": len(updated_records),
            "requests": [record.model_dump(mode="json") for record in updated_records],
        },
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return 202, accepted_payload


def _handle_every_code_pull_request_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> _EveryCodeWebhookResponse:
    if payload.get("action") != "closed":
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    pull_request_payload = _github_webhook_mapping(payload, "pull_request")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    if not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    pr_url = _github_webhook_raw_string(pull_request_payload, "html_url")
    if not pr_url.strip() or pr_url.strip() != pr_url:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    linked_issue_numbers = (
        _github_issue_numbers_referenced_by_pull_request(
            pull_request_payload,
            repository=repository,
        )
        if pull_request_payload is not None
        else frozenset()
    )
    merged = bool(pull_request_payload.get("merged")) if pull_request_payload else False
    closed_at = (
        _github_webhook_string(pull_request_payload, "closed_at") or dependencies.now_timestamp()
    )
    every_code_store = _every_code_work_request_store(record_store)
    candidate_records: dict[str, EveryCodeWorkRequestRecord] = {}
    repository_records = tuple(
        _iter_every_code_work_request_records(every_code_store, repository=repository)
    )
    for record in repository_records:
        if record.result_pr_url.strip() == pr_url:
            candidate_records[record.request_id] = record

    feedback_record = _find_every_code_pr_feedback_for_pull_request(
        every_code_store,
        repository=repository,
        pr_url=pr_url,
    )
    if feedback_record is not None:
        feedback_request_record = every_code_store.read_every_code_work_request_record(
            feedback_record.request_id
        )
        candidate_records[feedback_request_record.request_id] = feedback_request_record

    for record in repository_records:
        if _every_code_issue_url_matches_pull_request(
            issue_url=record.issue_url,
            repository=repository,
            pr_url=pr_url,
            linked_issue_numbers=linked_issue_numbers,
        ):
            candidate_records[record.request_id] = record

    if not candidate_records:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_not_found",
                "github_delivery_id": delivery_id,
            },
        )

    updated_records: list[EveryCodeWorkRequestRecord] = []
    terminal_records: list[EveryCodeWorkRequestRecord] = []
    for record in candidate_records.values():
        closed_record = close_every_code_work_request_for_pull_request(
            record,
            pr_url=pr_url,
            merged=merged,
            closed_at=closed_at,
        )
        if closed_record is None:
            terminal_records.append(record)
            continue
        every_code_store.write_every_code_work_request_record(closed_record)
        updated_records.append(closed_record)

    if not updated_records:
        terminal_record = terminal_records[0]
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_already_terminal",
                "result": {
                    "request_id": terminal_record.request_id,
                    "state": terminal_record.state,
                },
                "github_delivery_id": delivery_id,
            },
        )

    updated_record = updated_records[0]
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "request_id": updated_record.request_id,
            "state": updated_record.state,
            "closed_count": len(updated_records),
        },
        driver_result={
            "request": updated_record.model_dump(mode="json"),
            "closed_count": len(updated_records),
            "requests": [record.model_dump(mode="json") for record in updated_records],
        },
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return 202, accepted_payload


def _every_code_issue_url_matches_pull_request(
    *,
    issue_url: str,
    repository: str,
    pr_url: str,
    linked_issue_numbers: frozenset[int],
) -> bool:
    normalized_issue_url = issue_url.strip().rstrip("/").lower()
    normalized_pr_url = pr_url.strip().rstrip("/").lower()
    if not normalized_issue_url or not normalized_pr_url:
        return False
    if normalized_issue_url == normalized_pr_url:
        return True
    normalized_repository = repository.strip().strip("/").lower()
    if not normalized_repository:
        return False
    return any(
        normalized_issue_url == f"https://github.com/{normalized_repository}/issues/{issue_number}"
        for issue_number in linked_issue_numbers
    )


def _handle_every_code_preview_validation_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root_path: Path,
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> _EveryCodeWebhookResponse | None:
    if payload.get("action") != "created":
        return None
    issue_payload = _github_webhook_mapping(payload, "issue")
    if issue_payload is None or isinstance(issue_payload.get("pull_request"), dict):
        return None
    repository_payload = _github_webhook_mapping(payload, "repository")
    repository = _github_webhook_string(repository_payload, "full_name")
    if "/" not in repository:
        return None
    owner, repo = repository.split("/", 1)
    issue_number_value = issue_payload.get("number")
    if not isinstance(issue_number_value, int):
        return None
    issue_author_payload = _github_webhook_mapping(issue_payload, "user")
    issue_author = _github_webhook_string(issue_author_payload, "login")
    actor = _github_actor_login(payload)
    comment_payload = _github_webhook_mapping(payload, "comment")
    if comment_payload is None:
        return None
    comment_body = _github_webhook_string(comment_payload, "body")
    if not comment_body.strip().lower().startswith("/preview"):
        return None
    if not _every_code_feedback_actor_is_trusted(
        repository=repository,
        actor=actor,
        source_issue_author=issue_author,
    ):
        return _every_code_untrusted_feedback_response(
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    every_code_store = _every_code_work_request_store(record_store)
    context_name = dependencies.anchor_repo_context(
        record_store=cast(ProductProfileListStore, record_store), repo=repo
    )
    if not context_name:
        context_name = f"{repo}-preview"
    try:
        token = dependencies.github_token(
            control_plane_root=control_plane_root_path,
            context_name=context_name,
        )
        result = handle_every_code_preview_validation_comment(
            record_store=every_code_store,
            owner=owner,
            repo=repo,
            issue_number=issue_number_value,
            issue_url=_github_webhook_string(issue_payload, "html_url"),
            issue_author=issue_author,
            actor=actor,
            comment_body=comment_body,
            comment_id=str(comment_payload.get("id") or ""),
            comment_node_id=_github_webhook_string(comment_payload, "node_id"),
            comment_url=_github_webhook_string(comment_payload, "html_url"),
            delivery_id=delivery_id,
            token=token,
            received_at=dependencies.now_timestamp(),
        )
    except click.ClickException:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "preview_validation_failed",
                "github_delivery_id": delivery_id,
            },
        )
    if not bool(result.get("handled")):
        return None
    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "preview_validation": {key: value for key, value in result.items() if key != "handled"}
        },
        driver_result={"preview_validation": dict(result)},
    )
    if bool(result.get("skipped")):
        accepted_payload["skipped"] = True
        reason = result.get("reason")
        if isinstance(reason, str):
            accepted_payload["reason"] = reason
    if bool(result.get("deduped")):
        accepted_payload["deduped"] = True
    accepted_payload["github_delivery_id"] = delivery_id
    return 202, accepted_payload


def _github_issue_numbers_referenced_by_pull_request(
    pull_request_payload: dict[str, object],
    *,
    repository: str,
) -> frozenset[int]:
    normalized_repository = repository.strip().strip("/").lower()
    if not normalized_repository:
        return frozenset()

    issue_numbers: set[int] = set()
    for field in ("title", "body"):
        value = _github_webhook_string(pull_request_payload, field)
        if not value:
            continue
        for closing_reference_match in _GITHUB_CLOSING_REFERENCE_PATTERN.finditer(value):
            references = closing_reference_match.group(1)
            for issue_reference_match in _GITHUB_ISSUE_REFERENCE_PATTERN.finditer(references):
                reference_repository = (
                    issue_reference_match.group("url_repository")
                    or issue_reference_match.group("repository")
                    or normalized_repository
                ).lower()
                if reference_repository != normalized_repository:
                    continue
                issue_number = issue_reference_match.group(
                    "url_number"
                ) or issue_reference_match.group("number")
                issue_numbers.add(int(issue_number))
    return frozenset(issue_numbers)


def _find_every_code_pr_feedback_for_pull_request(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
    pr_url: str,
) -> EveryCodePrFeedbackRecord | None:
    for record in _iter_every_code_pr_feedback_records(
        every_code_store,
        repository=repository,
    ):
        if record.pr_url.strip() == pr_url:
            return record
    return None


def _iter_every_code_work_request_records(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
) -> Iterable[EveryCodeWorkRequestRecord]:
    page_size = 100
    offset = 0
    while True:
        records = every_code_store.list_every_code_work_request_records(
            repository=repository,
            limit=page_size,
            offset=offset,
        )
        if not records:
            break
        yield from records
        if len(records) < page_size:
            break
        offset += page_size


def _iter_every_code_pr_feedback_records(
    every_code_store: _EveryCodeWorkRequestStore,
    *,
    repository: str,
) -> Iterable[EveryCodePrFeedbackRecord]:
    page_size = 100
    offset = 0
    while True:
        records = every_code_store.list_every_code_pr_feedback_records(
            repository=repository,
            limit=page_size,
            offset=offset,
        )
        if not records:
            break
        yield from records
        if len(records) < page_size:
            break
        offset += page_size


def _handle_every_code_pr_feedback_webhook(
    *,
    trace_id: str,
    delivery_id: str,
    event_name: str,
    payload: dict[str, object],
    record_store: object,
    dependencies: EveryCodeGitHubWebhookDependencies,
) -> _EveryCodeWebhookResponse:
    if not _every_code_pr_feedback_action_supported(
        event_name=event_name,
        action=str(payload.get("action", "")),
    ):
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "unsupported_action",
                "github_delivery_id": delivery_id,
            },
        )

    sender_payload = _github_webhook_mapping(payload, "sender")
    body_payload = _every_code_feedback_body_payload(event_name=event_name, payload=payload)
    if body_payload is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    if _every_code_feedback_actor_is_automation(
        sender_payload=sender_payload,
        body_payload=body_payload,
    ):
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "automation_actor",
                "github_delivery_id": delivery_id,
            },
        )

    repository_payload = _github_webhook_mapping(payload, "repository")
    repository = _github_webhook_raw_string(repository_payload, "full_name")
    if not _github_repository_full_name_is_valid(repository):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    actor = _github_webhook_string(sender_payload, "login")
    if not _every_code_feedback_actor_is_trusted(repository=repository, actor=actor):
        return _every_code_untrusted_feedback_response(
            trace_id=trace_id,
            delivery_id=delivery_id,
        )
    pr_reference = _every_code_feedback_pr_reference(
        event_name=event_name,
        payload=payload,
        repository=repository,
    )
    if pr_reference is None:
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    pr_number, pr_url = pr_reference
    body = _github_webhook_string(body_payload, "body")
    if not body.strip():
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "empty_feedback_body",
                "github_delivery_id": delivery_id,
            },
        )

    every_code_store = _every_code_work_request_store(record_store)
    matched_record = next(
        (
            record
            for record in _iter_every_code_work_request_records(
                every_code_store,
                repository=repository,
            )
            if record.result_pr_url.strip() == pr_url
        ),
        None,
    )
    if matched_record is None:
        pull_request_payload = _github_webhook_mapping(payload, "pull_request")
        linked_issue_numbers: frozenset[int] = frozenset()
        if pull_request_payload is not None:
            linked_issue_numbers = _github_issue_numbers_referenced_by_pull_request(
                pull_request_payload, repository=repository
            )
        if event_name == "issue_comment":
            issue_payload = _github_webhook_mapping(payload, "issue")
            if issue_payload is not None:
                linked_issue_numbers = (
                    linked_issue_numbers
                    | _github_issue_numbers_referenced_by_pull_request(
                        issue_payload, repository=repository
                    )
                )
        for record in _iter_every_code_work_request_records(
            every_code_store,
            repository=repository,
        ):
            if _every_code_issue_url_matches_pull_request(
                issue_url=record.issue_url,
                repository=repository,
                pr_url=pr_url,
                linked_issue_numbers=linked_issue_numbers,
            ):
                matched_record = record
                break
    if matched_record is None:
        return (
            202,
            {
                "status": "accepted",
                "trace_id": trace_id,
                "skipped": True,
                "reason": "linked_every_code_request_not_found",
                "github_delivery_id": delivery_id,
            },
        )

    github_node_id = _github_webhook_string(body_payload, "node_id")
    github_id_value = body_payload.get("id")
    github_id = str(github_id_value) if github_id_value is not None else ""
    github_feedback_identity = github_node_id.strip() or github_id.strip()
    if not github_feedback_identity or not any(
        character.isalnum() for character in github_feedback_identity
    ):
        return _every_code_github_webhook_invalid_payload_response(trace_id)
    feedback_id = build_every_code_pr_feedback_id(
        repository=repository,
        pr_number=pr_number,
        github_delivery_id=delivery_id,
        github_node_id=github_node_id,
        github_id=github_id,
    )
    existing_feedback_records = every_code_store.list_every_code_pr_feedback_records(
        request_id=matched_record.request_id,
        repository=repository,
        pr_number=pr_number,
        limit=100,
    )
    for existing_feedback in existing_feedback_records:
        if existing_feedback.feedback_id == feedback_id:
            return (
                202,
                {
                    "status": "accepted",
                    "trace_id": trace_id,
                    "deduped": True,
                    "result": {
                        "feedback_id": existing_feedback.feedback_id,
                        "request_id": existing_feedback.request_id,
                    },
                    "github_delivery_id": delivery_id,
                },
            )

    feedback_record = EveryCodePrFeedbackRecord(
        feedback_id=feedback_id,
        request_id=matched_record.request_id,
        repository=repository,
        pr_number=pr_number,
        pr_url=pr_url,
        feedback_kind=_every_code_feedback_kind(event_name),
        github_delivery_id=delivery_id,
        github_node_id=github_node_id,
        github_id=github_id,
        actor=actor,
        author_association=_github_webhook_string(body_payload, "author_association"),
        body=body,
        html_url=_github_webhook_string(body_payload, "html_url"),
        submitted_at=_github_webhook_string(body_payload, "submitted_at"),
        received_at=dependencies.now_timestamp(),
    )
    every_code_store.write_every_code_pr_feedback_record(feedback_record)

    accepted_payload = _accepted_payload(
        trace_id=trace_id,
        result={
            "feedback_id": feedback_record.feedback_id,
            "request_id": feedback_record.request_id,
            "status": feedback_record.status,
        },
        driver_result={"feedback": feedback_record.model_dump(mode="json")},
    )
    accepted_payload["github_delivery_id"] = delivery_id
    return 202, accepted_payload


def _every_code_pr_feedback_action_supported(*, event_name: str, action: str) -> bool:
    if event_name == "issue_comment":
        return action == "created"
    if event_name == "pull_request_review":
        return action == "submitted"
    if event_name == "pull_request_review_comment":
        return action == "created"
    return False


def _every_code_feedback_actor_is_automation(
    *,
    sender_payload: dict[str, object] | None,
    body_payload: dict[str, object],
) -> bool:
    actors = [sender_payload]
    user_payload = _github_webhook_mapping(body_payload, "user")
    if user_payload is not None:
        actors.append(user_payload)
    for actor_payload in actors:
        if actor_payload is None:
            continue
        actor_type = _github_webhook_string(actor_payload, "type").lower()
        actor_login = _github_webhook_string(actor_payload, "login").lower()
        if actor_type == "bot" or actor_login.endswith("[bot]"):
            return True
    return False


def _every_code_feedback_kind(event_name: str) -> EveryCodePrFeedbackKind:
    if event_name == "issue_comment":
        return "issue_comment"
    if event_name == "pull_request_review":
        return "pull_request_review"
    if event_name == "pull_request_review_comment":
        return "pull_request_review_comment"
    raise ValueError(f"Unsupported Every Code PR feedback event {event_name!r}")


def _every_code_feedback_body_payload(
    *, event_name: str, payload: dict[str, object]
) -> dict[str, object] | None:
    key = "review" if event_name == "pull_request_review" else "comment"
    return _github_webhook_mapping(payload, key)


def _every_code_feedback_pr_reference(
    *,
    event_name: str,
    payload: dict[str, object],
    repository: str,
) -> tuple[int, str] | None:
    if event_name == "issue_comment":
        issue_payload = _github_webhook_mapping(payload, "issue")
        if issue_payload is None:
            return None
        pull_request_marker = issue_payload.get("pull_request")
        if not isinstance(pull_request_marker, dict):
            return None
        pr_number_value = _github_webhook_positive_int(issue_payload, "number")
    else:
        pull_request_payload = _github_webhook_mapping(payload, "pull_request")
        if pull_request_payload is None:
            return None
        pr_number_value = _github_webhook_positive_int(pull_request_payload, "number")
    if pr_number_value is None:
        return None
    return pr_number_value, f"https://github.com/{repository}/pull/{pr_number_value}"


def _accepted_payload(
    *,
    trace_id: str,
    result: dict[str, object],
    driver_result: BaseModel | dict[str, object] | None,
    extra_record_keys: frozenset[str] = frozenset(),
    replayed: bool = False,
    original_trace_id: str = "",
) -> dict[str, object]:
    serialized_driver_result: dict[str, object] | None = None
    if isinstance(driver_result, BaseModel):
        serialized_driver_result = driver_result.model_dump(mode="json")
    elif isinstance(driver_result, dict):
        serialized_driver_result = dict(driver_result)
    record_keys = {
        "deployment_record_id",
        "backup_gate_record_id",
        "backup_record_id",
        "release_tuple_id",
        "inventory_record_id",
        "preview_id",
        "preview_desired_state_id",
        "preview_inventory_scan_id",
        "preview_pr_feedback_id",
        "preview_lifecycle_cleanup_id",
        "preview_lifecycle_plan_id",
        "authz_policy_record_id",
        "runtime_key_safety_policy_record_id",
        "product_profile",
        "provider_target_count",
        "provider_target_id_count",
        "runtime_environment_record_count",
        "secret_binding_count",
        "generation_id",
        "promotion_record_id",
        "target_id",
        "target_type",
        "image_reference",
        "artifact_id",
        "transition",
        "preview_state",
        "preview_generation_id",
        "verification_status",
        "verified_at",
        "generic_web_preview_verification",
        "request_id",
        "feedback_id",
        "state",
        "merge_train_batch_candidate_record_id",
        "merge_train_batch_landing_plan_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "merge_train_run_id",
        "odoo_stable_bootstrap_operation_id",
        "odoo_stable_target_replacement_operation_id",
        "runner_host_hygiene_audit_record_key",
        "runner_lane_registration_audit_record_key",
        "generic_web_rollback_plan_id",
        "ingress_route_audit_record_id",
        "edge_endpoint_key",
        "ingress_canary_route_key",
    }
    accepted_record_keys = record_keys | extra_record_keys
    records: dict[str, object] = {}
    for key, value in result.items():
        if key not in accepted_record_keys:
            continue
        if key.endswith("_preview_verification") and isinstance(value, dict):
            records[key] = value
            continue
        records[key] = str(value)
    payload: dict[str, object] = {
        "status": "accepted",
        "trace_id": trace_id,
        "records": records,
        **({"result": serialized_driver_result} if serialized_driver_result else {}),
    }
    if replayed:
        payload["replayed"] = True
        payload["original_trace_id"] = original_trace_id
    return payload


def _decode_json_request_body_or_none(body_bytes: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)
