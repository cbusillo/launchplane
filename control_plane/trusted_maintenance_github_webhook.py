from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.trusted_maintenance import TrustedMaintenancePolicyRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.trusted_maintenance import (
    TrustedMaintenanceAuthorityError,
    TrustedMaintenanceAuthorityReadStore,
    TrustedMaintenanceEvidenceConflictError,
    TrustedMaintenanceExpectedAuthority,
    TrustedMaintenanceGitHubEventFacts,
    TrustedMaintenanceRuleMatchError,
    trusted_maintenance_current_authority,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
)


TRUSTED_MAINTENANCE_GITHUB_WEBHOOK_SOURCE = "github-webhook"

TrustedMaintenanceWebhookStatus = Literal[
    "captured",
    "replayed",
    "skipped",
    "conflict",
    "retryable_error",
]


class _GitHubTokenResolver(Protocol):
    def __call__(self, *, control_plane_root: Path, context_name: str) -> str: ...


class _GitHubApiRequest(Protocol):
    def __call__(
        self,
        *,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class TrustedMaintenanceGitHubWebhookDependencies:
    github_token: _GitHubTokenResolver = resolve_launchplane_github_token
    github_api: _GitHubApiRequest = github_api_request


@dataclass(frozen=True)
class TrustedMaintenanceGitHubWebhookResult:
    status: TrustedMaintenanceWebhookStatus
    reason: str = ""
    evidence_status: Literal["written", "replayed", ""] = ""


def handle_trusted_maintenance_github_webhook(
    *,
    event_name: str,
    delivery_id: str,
    payload: dict[str, object],
    record_store: object,
    control_plane_root: Path,
    dependencies: TrustedMaintenanceGitHubWebhookDependencies | None = None,
) -> TrustedMaintenanceGitHubWebhookResult:
    """Capture trusted-maintenance evidence after the caller verifies GitHub signature."""

    normalized_event_name = event_name.strip().lower()
    normalized_delivery_id = delivery_id.strip()
    if normalized_event_name != "pull_request":
        return _skipped("unsupported_event")
    if not normalized_delivery_id:
        return _skipped("missing_delivery")

    signed = _signed_pull_request_delivery(
        payload=payload,
        event_name=normalized_event_name,
        delivery_id=normalized_delivery_id,
    )
    if signed is None:
        return _skipped("malformed_payload")
    if signed.sender_type != "Bot" or signed.pr_author_type != "Bot":
        return _skipped("non_bot_actor")
    try:
        authority = _preflight_current_authority(
            record_store=record_store,
            signed=signed,
        )
    except Exception:
        return _retryable("database_unavailable")
    if authority is None:
        return _skipped("authority_not_available")
    if (
        _matching_rule_candidate_count(
            policy_record=authority.policy_record,
            signed=signed,
        )
        != 1
    ):
        return _skipped("rule_not_matched")
    if (
        not isinstance(record_store, PostgresRecordStore)
        or record_store.database_dialect_name != "postgresql"
    ):
        return _retryable("database_storage_required")

    resolved_dependencies = dependencies or TrustedMaintenanceGitHubWebhookDependencies()
    try:
        token = resolved_dependencies.github_token(
            control_plane_root=control_plane_root,
            context_name=authority.classification.context,
        )
    except click.ClickException:
        return _retryable("github_token_unavailable")
    if not token.strip():
        return _retryable("github_token_unavailable")
    try:
        pull_request = _fetch_github_pull_request(
            api_request=resolved_dependencies.github_api,
            token=token,
            repository=signed.repository,
            pr_number=signed.pull_request_number,
        )
    except click.ClickException:
        return _retryable("github_api_unavailable")

    current = _validated_current_pull_request_facts(signed=signed, pull_request=pull_request)
    if current is None:
        return _skipped("current_pull_request_not_matched")

    candidate = TenantMergeCandidate(
        product=authority.classification.product,
        context=authority.classification.context,
        repository_id=signed.repository_id,
        repository_owner_id=signed.repository_owner_id,
        repository=signed.repository,
        pull_request_number=signed.pull_request_number,
        head_sha=signed.head_sha,
    )
    event_facts = TrustedMaintenanceGitHubEventFacts(
        pr_author_github_id=current.pr_author_github_id,
        pr_author_type=current.pr_author_type,
        pr_author_login=current.pr_author_login,
        sender_github_id=signed.sender_github_id,
        sender_type=signed.sender_type,
        sender_login=signed.sender_login,
        head_repository_id=current.head_repository_id,
        head_repository_owner_id=current.head_repository_owner_id,
        head_repository=current.head_repository,
        event_name=signed.event_name,
        event_action=signed.action,
        source=TRUSTED_MAINTENANCE_GITHUB_WEBHOOK_SOURCE,
        delivery_id=signed.delivery_id,
    )
    expected_authority = TrustedMaintenanceExpectedAuthority(
        classification_record_id=authority.classification.record_id,
        classification_revision=authority.classification.classification_revision,
        classification_digest=authority.classification.classification_digest,
        policy_record_id=authority.policy_record.record_id,
        policy_revision=authority.policy_record.policy_revision,
        policy_digest=authority.policy_record.policy_digest,
    )
    try:
        evidence_status = record_store.capture_trusted_maintenance_evidence_transactionally(
            candidate=candidate,
            expected_authority=expected_authority,
            event_facts=event_facts,
        )
    except TrustedMaintenanceRuleMatchError:
        return _skipped("rule_not_matched")
    except TrustedMaintenanceEvidenceConflictError:
        return TrustedMaintenanceGitHubWebhookResult(
            status="conflict",
            reason="evidence_conflict",
        )
    except TrustedMaintenanceAuthorityError:
        return _retryable("authority_drift")
    except ValueError:
        return _retryable("database_unavailable")
    except Exception:
        return _retryable("database_unavailable")
    if evidence_status == "replayed":
        return TrustedMaintenanceGitHubWebhookResult(
            status="replayed",
            reason="evidence_replayed",
            evidence_status="replayed",
        )
    return TrustedMaintenanceGitHubWebhookResult(
        status="captured",
        reason="evidence_captured",
        evidence_status="written",
    )


@dataclass(frozen=True)
class _SignedPullRequestDelivery:
    event_name: str
    action: str
    delivery_id: str
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    pr_author_github_id: int
    pr_author_type: str
    pr_author_login: str
    sender_github_id: int
    sender_type: str
    sender_login: str


@dataclass(frozen=True)
class _PreflightAuthority:
    classification: TenantRepositoryClassificationRecord
    policy_record: TrustedMaintenancePolicyRecord


@dataclass(frozen=True)
class _CurrentPullRequestFacts:
    pr_author_github_id: int
    pr_author_type: str
    pr_author_login: str
    head_repository_id: str
    head_repository_owner_id: str
    head_repository: str


def _signed_pull_request_delivery(
    *,
    payload: dict[str, object],
    event_name: str,
    delivery_id: str,
) -> _SignedPullRequestDelivery | None:
    repository_payload = _mapping(payload, "repository")
    owner_payload = _mapping(repository_payload, "owner")
    pull_request_payload = _mapping(payload, "pull_request")
    sender_payload = _mapping(payload, "sender")
    repository_id = _positive_id_string(repository_payload, "id")
    repository_owner_id = _positive_id_string(owner_payload, "id")
    repository = _string(repository_payload, "full_name").lower()
    pull_request_number = _positive_int(pull_request_payload, "number")
    head_sha = _nested_string(pull_request_payload, "head", "sha").lower()
    pr_author_payload = _mapping(pull_request_payload, "user")
    pr_author_github_id = _positive_int(pr_author_payload, "id")
    pr_author_type = _string(pr_author_payload, "type")
    pr_author_login = _string(pr_author_payload, "login")
    sender_github_id = _positive_int(sender_payload, "id")
    sender_type = _string(sender_payload, "type")
    sender_login = _string(sender_payload, "login")
    action = _string(payload, "action").lower()
    if (
        not repository_id
        or not repository_owner_id
        or not _valid_repository(repository)
        or pull_request_number is None
        or not _valid_git_sha(head_sha)
        or pr_author_github_id is None
        or not pr_author_type
        or not pr_author_login
        or sender_github_id is None
        or not sender_type
        or not sender_login
        or not action
    ):
        return None
    return _SignedPullRequestDelivery(
        event_name=event_name,
        action=action,
        delivery_id=delivery_id,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        pr_author_github_id=pr_author_github_id,
        pr_author_type=pr_author_type,
        pr_author_login=pr_author_login,
        sender_github_id=sender_github_id,
        sender_type=sender_type,
        sender_login=sender_login,
    )


def _preflight_current_authority(
    *,
    record_store: object,
    signed: _SignedPullRequestDelivery,
) -> _PreflightAuthority | None:
    if not all(
        hasattr(record_store, method_name)
        for method_name in (
            "list_tenant_repository_classification_records",
            "list_trusted_maintenance_policy_records",
        )
    ):
        return None
    authority_store = cast(TrustedMaintenanceAuthorityReadStore, record_store)
    classifications = authority_store.list_tenant_repository_classification_records(
        repository_id=signed.repository_id,
    )
    repository_classifications = tuple(
        record for record in classifications if record.repository_id == signed.repository_id
    )
    if not repository_classifications:
        return None
    highest_revision = max(record.classification_revision for record in repository_classifications)
    current_classifications = tuple(
        record
        for record in repository_classifications
        if record.classification_revision == highest_revision
    )
    if len(current_classifications) != 1:
        return None
    classification = current_classifications[0]
    if (
        classification.repository_owner_id != signed.repository_owner_id
        or classification.repository != signed.repository
        or classification.classification_kind != "tenant_ui"
    ):
        return None
    candidate = TenantMergeCandidate(
        product=classification.product,
        context=classification.context,
        repository_id=signed.repository_id,
        repository_owner_id=signed.repository_owner_id,
        repository=signed.repository,
        pull_request_number=signed.pull_request_number,
        head_sha=signed.head_sha,
    )
    try:
        current = trusted_maintenance_current_authority(
            store=authority_store,
            candidate=candidate,
            evaluated_at=utc_now_timestamp(),
        )
    except TrustedMaintenanceAuthorityError:
        return None
    return _PreflightAuthority(
        classification=current.classification,
        policy_record=current.policy_record,
    )


def _matching_rule_candidate_count(
    *, policy_record: TrustedMaintenancePolicyRecord, signed: _SignedPullRequestDelivery
) -> int:
    rule_count = 0
    for rule in getattr(policy_record, "actor_rules", ()):  # pydantic model in production
        if getattr(rule, "actor_github_id", None) != signed.pr_author_github_id:
            continue
        if getattr(rule, "actor_type", "") != "Bot":
            continue
        if signed.sender_github_id not in getattr(rule, "sender_github_ids", ()):
            continue
        if getattr(rule, "sender_type", "") != "Bot":
            continue
        for allowed in getattr(rule, "allowed_events", ()):
            if getattr(allowed, "event_name", "") == signed.event_name and signed.action in getattr(
                allowed, "actions", ()
            ):
                rule_count += 1
                break
    return rule_count


def _fetch_github_pull_request(
    *,
    api_request: _GitHubApiRequest,
    token: str,
    repository: str,
    pr_number: int,
) -> dict[str, object]:
    payload = api_request(path=f"/repos/{repository}/pulls/{pr_number}", token=token)
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub pull request response must be an object.")
    return cast(dict[str, object], payload)


def _validated_current_pull_request_facts(
    *,
    signed: _SignedPullRequestDelivery,
    pull_request: dict[str, object],
) -> _CurrentPullRequestFacts | None:
    if _string(pull_request, "state").lower() != "open":
        return None
    base_repo = _mapping(_mapping(pull_request, "base"), "repo")
    base_owner = _mapping(base_repo, "owner")
    if (
        _positive_id_string(base_repo, "id") != signed.repository_id
        or _positive_id_string(base_owner, "id") != signed.repository_owner_id
        or _string(base_repo, "full_name").lower() != signed.repository
    ):
        return None
    author = _mapping(pull_request, "user")
    author_id = _positive_int(author, "id")
    author_type = _string(author, "type")
    author_login = _string(author, "login")
    if author_id is None or author_type != "Bot" or not author_login:
        return None
    if author_id != signed.pr_author_github_id or author_type != signed.pr_author_type:
        return None
    head = _mapping(pull_request, "head")
    if _string(head, "sha").lower() != signed.head_sha:
        return None
    head_repo = _mapping(head, "repo")
    head_owner = _mapping(head_repo, "owner")
    head_repository_id = _positive_id_string(head_repo, "id")
    head_repository_owner_id = _positive_id_string(head_owner, "id")
    head_repository = _string(head_repo, "full_name").lower()
    if (
        head_repository_id != signed.repository_id
        or head_repository_owner_id != signed.repository_owner_id
        or head_repository != signed.repository
    ):
        return None
    return _CurrentPullRequestFacts(
        pr_author_github_id=author_id,
        pr_author_type=author_type,
        pr_author_login=author_login,
        head_repository_id=head_repository_id,
        head_repository_owner_id=head_repository_owner_id,
        head_repository=head_repository,
    )


def _mapping(mapping: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _nested_string(mapping: dict[str, object] | None, parent: str, key: str) -> str:
    return _string(_mapping(mapping, parent), key)


def _positive_int(mapping: dict[str, object] | None, key: str) -> int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _positive_id_string(mapping: dict[str, object] | None, key: str) -> str:
    value = _positive_int(mapping, key)
    return str(value) if value is not None else ""


def _valid_repository(repository: str) -> bool:
    if repository.strip() != repository or repository.lower() != repository:
        return False
    owner, separator, name = repository.partition("/")
    return bool(separator and owner and name and "/" not in name)


def _valid_git_sha(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _skipped(reason: str) -> TrustedMaintenanceGitHubWebhookResult:
    return TrustedMaintenanceGitHubWebhookResult(status="skipped", reason=reason)


def _retryable(reason: str) -> TrustedMaintenanceGitHubWebhookResult:
    return TrustedMaintenanceGitHubWebhookResult(status="retryable_error", reason=reason)
