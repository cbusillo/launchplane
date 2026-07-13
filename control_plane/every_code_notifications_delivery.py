from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationDeliveryStatus,
    EveryCodeNotificationDestination,
    EveryCodeNotificationEvent,
    EveryCodeNotificationPolicyRecord,
    build_every_code_notification_attempt_id,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.notifications import post_discord_webhook, public_discord_url_error

_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class EveryCodeNotificationStore(Protocol):
    def write_every_code_notification_policy_record(
        self, record: EveryCodeNotificationPolicyRecord
    ) -> object: ...

    def list_every_code_notification_policy_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationPolicyRecord, ...]: ...

    def write_every_code_notification_attempt_record(
        self, record: EveryCodeNotificationAttemptRecord
    ) -> object: ...

    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]: ...


def deliver_every_code_blocked_notifications(
    *,
    record_store: object,
    request: EveryCodeWorkRequestRecord,
    attempted_at: str,
    discord_sender: Callable[[str, dict[str, object]], object] = post_discord_webhook,
) -> tuple[EveryCodeNotificationAttemptRecord, ...]:
    notification_store = _every_code_notification_store(record_store)
    secret_store = _secret_capable_store(record_store)
    if notification_store is None or secret_store is None:
        return ()
    policies = tuple(
        policy
        for policy in notification_store.list_every_code_notification_policy_records(
            repository=request.repository,
            status="enabled",
            limit=None,
        )
        if policy.matches(request)
    )
    if not policies:
        return ()
    secret_resolver = _launchplane_managed_secret_resolver(
        record_store=secret_store,
        context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        instance_name="every-code",
    )
    attempts: list[EveryCodeNotificationAttemptRecord] = []
    for policy in policies:
        for destination in policy.destinations:
            if destination.status != "enabled":
                attempt = _write_every_code_notification_attempt(
                    notification_store=notification_store,
                    request=request,
                    event="work_request_blocked",
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                    delivery_status="skipped",
                    action="destination_disabled",
                )
                attempts.append(attempt)
                continue
            if destination.kind == "discord":
                attempt = deliver_every_code_discord_notification(
                    notification_store=notification_store,
                    secret_resolver=secret_resolver,
                    discord_sender=discord_sender,
                    request=request,
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                )
                attempts.append(attempt)
    return tuple(attempts)


def deliver_every_code_discord_notification(
    *,
    notification_store: EveryCodeNotificationStore,
    secret_resolver: Callable[[str], str],
    discord_sender: Callable[[str, dict[str, object]], object],
    request: EveryCodeWorkRequestRecord,
    policy: EveryCodeNotificationPolicyRecord,
    destination: EveryCodeNotificationDestination,
    attempted_at: str,
) -> EveryCodeNotificationAttemptRecord:
    webhook_url = secret_resolver(destination.discord_webhook_secret).strip()
    if not webhook_url:
        return _write_every_code_notification_attempt(
            notification_store=notification_store,
            request=request,
            event="work_request_blocked",
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="missing_discord_webhook",
            error_message="Discord webhook secret could not be resolved.",
        )
    public_url_error = public_discord_url_error(webhook_url)
    if public_url_error:
        return _write_every_code_notification_attempt(
            notification_store=notification_store,
            request=request,
            event="work_request_blocked",
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="invalid_discord_webhook",
            error_message=f"Discord webhook URL is not public: {public_url_error}",
        )
    existing_attempt = _existing_every_code_notification_attempt(
        notification_store=notification_store,
        request=request,
        event="work_request_blocked",
        policy=policy,
        destination=destination,
    )
    if existing_attempt is not None and existing_attempt.delivery_status in {
        "pending",
        "delivered",
    }:
        return existing_attempt
    pending_attempt = _write_every_code_notification_attempt(
        notification_store=notification_store,
        request=request,
        event="work_request_blocked",
        policy=policy,
        destination=destination,
        attempted_at=attempted_at,
        delivery_status="pending",
        action="dispatching_discord",
    )
    try:
        discord_sender(webhook_url, _every_code_blocked_discord_payload(request))
    except Exception as error:  # noqa: BLE001 - delivery attempt records preserve failure detail.
        return _write_every_code_notification_attempt(
            notification_store=notification_store,
            request=request,
            event="work_request_blocked",
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="discord_webhook_failed",
            error_message=str(error) or error.__class__.__name__,
        )
    try:
        return _write_every_code_notification_attempt(
            notification_store=notification_store,
            request=request,
            event="work_request_blocked",
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="delivered",
            action="posted_discord",
        )
    except Exception:  # noqa: BLE001 - the pending attempt preserves dispatch evidence.
        return pending_attempt


def _secret_capable_store(record_store: object) -> control_plane_secrets.SecretReadStore | None:
    if hasattr(record_store, "read_secret_record") and hasattr(record_store, "list_secret_records"):
        return cast(control_plane_secrets.SecretReadStore, record_store)
    return None


def _every_code_notification_store(record_store: object) -> EveryCodeNotificationStore | None:
    required_methods = (
        "write_every_code_notification_policy_record",
        "list_every_code_notification_policy_records",
        "write_every_code_notification_attempt_record",
        "list_every_code_notification_attempt_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(EveryCodeNotificationStore, record_store)
    return None


def _launchplane_managed_secret_resolver(
    *,
    record_store: control_plane_secrets.SecretReadStore,
    context_name: str,
    instance_name: str,
) -> Callable[[str], str]:
    def resolve(secret_id: str) -> str:
        normalized_secret_id = secret_id.strip()
        if not normalized_secret_id:
            return ""
        try:
            record = record_store.read_secret_record(normalized_secret_id)
        except Exception:  # noqa: BLE001 - notification attempts capture missing secrets.
            return ""
        if record.status != control_plane_secrets.SECRET_STATUS_CONFIGURED:
            return ""
        if not control_plane_secrets._scope_matches_record(
            record,
            context_name=context_name,
            instance_name=instance_name,
        ):
            return ""
        try:
            version = record_store.read_secret_version(record.current_version_id)
            return control_plane_secrets._decrypt_secret_value(version.ciphertext, version.key_id)
        except Exception:  # noqa: BLE001 - notification attempts capture unreadable secrets.
            return ""

    return resolve


def _existing_every_code_notification_attempt(
    *,
    notification_store: EveryCodeNotificationStore,
    request: EveryCodeWorkRequestRecord,
    event: EveryCodeNotificationEvent,
    policy: EveryCodeNotificationPolicyRecord,
    destination: EveryCodeNotificationDestination,
) -> EveryCodeNotificationAttemptRecord | None:
    attempt_id = build_every_code_notification_attempt_id(
        request_id=request.request_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        lifecycle_key=_every_code_notification_lifecycle_key(request),
    )
    return next(
        (
            attempt
            for attempt in notification_store.list_every_code_notification_attempt_records(
                request_id=request.request_id,
                event=event,
                limit=None,
            )
            if attempt.attempt_id == attempt_id
        ),
        None,
    )


def _write_every_code_notification_attempt(
    *,
    notification_store: EveryCodeNotificationStore,
    request: EveryCodeWorkRequestRecord,
    event: EveryCodeNotificationEvent,
    policy: EveryCodeNotificationPolicyRecord,
    destination: EveryCodeNotificationDestination,
    attempted_at: str,
    delivery_status: EveryCodeNotificationDeliveryStatus,
    action: str,
    error_message: str = "",
) -> EveryCodeNotificationAttemptRecord:
    attempt = EveryCodeNotificationAttemptRecord(
        attempt_id=build_every_code_notification_attempt_id(
            request_id=request.request_id,
            event=event,
            policy_id=policy.policy_id,
            destination_id=destination.destination_id,
            lifecycle_key=_every_code_notification_lifecycle_key(request),
        ),
        request_id=request.request_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        destination_kind=destination.kind,
        delivery_status=delivery_status,
        attempted_at=attempted_at,
        action=action,
        error_message=_bounded_text(error_message, max_length=500),
    )
    notification_store.write_every_code_notification_attempt_record(attempt)
    return attempt


def _every_code_notification_lifecycle_key(request: EveryCodeWorkRequestRecord) -> str:
    return request.lifecycle_id


def _every_code_blocked_discord_payload(
    request: EveryCodeWorkRequestRecord,
) -> dict[str, object]:
    fields = [
        {"name": "Repository", "value": request.repository, "inline": True},
        {"name": "Issue", "value": str(request.issue_number), "inline": True},
        {"name": "Host", "value": request.claimed_by_host, "inline": True},
        {"name": "Request", "value": request.request_id, "inline": False},
    ]
    if request.issue_url:
        fields.append({"name": "Issue URL", "value": request.issue_url, "inline": False})
    return {
        "embeds": [
            {
                "title": "Every Code work request blocked",
                "description": _bounded_text(request.error_message, max_length=1500),
                "color": 0xC62828,
                "fields": fields,
            }
        ]
    }


def _bounded_text(value: str, *, max_length: int) -> str:
    normalized_value = " ".join(value.strip().split())
    if len(normalized_value) <= max_length:
        return normalized_value
    return f"{normalized_value[: max_length - 3]}..."
