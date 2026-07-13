from collections.abc import Callable
from typing import Protocol, cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationDeliveryStatus,
    PreviewPrFeedbackNotificationDestination,
    PreviewPrFeedbackNotificationEvent,
    PreviewPrFeedbackNotificationPolicyRecord,
    build_preview_pr_feedback_notification_attempt_id,
    preview_pr_feedback_notification_event,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.notifications import post_discord_webhook, public_discord_url_error

_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class PreviewPrFeedbackNotificationStore(Protocol):
    def write_preview_pr_feedback_notification_policy_record(
        self, record: PreviewPrFeedbackNotificationPolicyRecord
    ) -> object: ...

    def list_preview_pr_feedback_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationPolicyRecord, ...]: ...

    def write_preview_pr_feedback_notification_attempt_record(
        self, record: PreviewPrFeedbackNotificationAttemptRecord
    ) -> object: ...

    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]: ...


def preview_pr_feedback_notification_store(
    record_store: object,
) -> PreviewPrFeedbackNotificationStore | None:
    required_methods = (
        "write_preview_pr_feedback_notification_policy_record",
        "list_preview_pr_feedback_notification_policy_records",
        "write_preview_pr_feedback_notification_attempt_record",
        "list_preview_pr_feedback_notification_attempt_records",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(PreviewPrFeedbackNotificationStore, record_store)
    return None


def secret_capable_store(record_store: object) -> control_plane_secrets.SecretReadStore | None:
    if hasattr(record_store, "read_secret_record") and hasattr(record_store, "list_secret_records"):
        return cast(control_plane_secrets.SecretReadStore, record_store)
    return None


def deliver_preview_pr_feedback_notifications(
    *,
    record_store: object,
    feedback: PreviewPrFeedbackRecord,
    attempted_at: str,
    discord_sender: Callable[[str, dict[str, object]], object] = post_discord_webhook,
) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]:
    if feedback.delivery_status not in {"skipped", "failed"}:
        return ()
    notification_store = preview_pr_feedback_notification_store(record_store)
    secret_store = secret_capable_store(record_store)
    if notification_store is None or secret_store is None:
        return ()
    policies = tuple(
        policy
        for policy in notification_store.list_preview_pr_feedback_notification_policy_records(
            product=feedback.product,
            context_name=feedback.context,
            repository=feedback.repository,
            status="enabled",
            limit=None,
        )
        if policy.matches(feedback)
    )
    if not policies:
        return ()
    event = preview_pr_feedback_notification_event(feedback)
    secret_resolver = launchplane_managed_secret_resolver(
        record_store=secret_store,
        context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        instance_name="preview-feedback",
    )
    attempts: list[PreviewPrFeedbackNotificationAttemptRecord] = []
    for policy in policies:
        for destination in policy.destinations:
            existing_attempt = existing_preview_pr_feedback_notification_attempt(
                notification_store=notification_store,
                feedback=feedback,
                event=event,
                policy=policy,
                destination=destination,
            )
            if existing_attempt is not None and existing_attempt.delivery_status in {
                "pending",
                "delivered",
                "skipped",
            }:
                attempts.append(existing_attempt)
                continue
            if destination.status != "enabled":
                attempt = write_preview_pr_feedback_notification_attempt(
                    notification_store=notification_store,
                    feedback=feedback,
                    event=event,
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                    delivery_status="skipped",
                    action="destination_disabled",
                )
                attempts.append(attempt)
                continue
            if destination.kind == "discord":
                attempt = deliver_preview_pr_feedback_discord_notification(
                    notification_store=notification_store,
                    secret_resolver=secret_resolver,
                    discord_sender=discord_sender,
                    feedback=feedback,
                    event=event,
                    policy=policy,
                    destination=destination,
                    attempted_at=attempted_at,
                )
                attempts.append(attempt)
    return tuple(attempts)


def deliver_preview_pr_feedback_discord_notification(
    *,
    notification_store: PreviewPrFeedbackNotificationStore,
    secret_resolver: Callable[[str], str],
    discord_sender: Callable[[str, dict[str, object]], object],
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
    attempted_at: str,
) -> PreviewPrFeedbackNotificationAttemptRecord:
    webhook_url = secret_resolver(destination.discord_webhook_secret).strip()
    if not webhook_url:
        return write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="missing_discord_webhook",
            error_message="Discord webhook secret could not be resolved.",
        )
    public_url_error = public_discord_url_error(webhook_url)
    if public_url_error:
        return write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="invalid_discord_webhook",
            error_message=f"Discord webhook URL is not public: {public_url_error}",
        )
    pending_attempt = write_preview_pr_feedback_notification_attempt(
        notification_store=notification_store,
        feedback=feedback,
        event=event,
        policy=policy,
        destination=destination,
        attempted_at=attempted_at,
        delivery_status="pending",
        action="dispatching_discord",
    )
    try:
        discord_sender(webhook_url, preview_pr_feedback_discord_payload(feedback, event=event))
    except Exception as error:  # noqa: BLE001 - delivery attempts preserve provider failures.
        return write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="failed",
            action="discord_webhook_failed",
            error_message=str(error) or error.__class__.__name__,
        )
    try:
        return write_preview_pr_feedback_notification_attempt(
            notification_store=notification_store,
            feedback=feedback,
            event=event,
            policy=policy,
            destination=destination,
            attempted_at=attempted_at,
            delivery_status="delivered",
            action="posted_discord",
        )
    except Exception:  # noqa: BLE001 - the pending attempt preserves dispatch evidence.
        return pending_attempt


def existing_preview_pr_feedback_notification_attempt(
    *,
    notification_store: PreviewPrFeedbackNotificationStore,
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
) -> PreviewPrFeedbackNotificationAttemptRecord | None:
    attempt_id = build_preview_pr_feedback_notification_attempt_id(
        feedback_id=feedback.feedback_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
    )
    return next(
        (
            attempt
            for attempt in notification_store.list_preview_pr_feedback_notification_attempt_records(
                feedback_id=feedback.feedback_id,
                event=event,
                limit=None,
            )
            if attempt.attempt_id == attempt_id
        ),
        None,
    )


def write_preview_pr_feedback_notification_attempt(
    *,
    notification_store: PreviewPrFeedbackNotificationStore,
    feedback: PreviewPrFeedbackRecord,
    event: PreviewPrFeedbackNotificationEvent,
    policy: PreviewPrFeedbackNotificationPolicyRecord,
    destination: PreviewPrFeedbackNotificationDestination,
    attempted_at: str,
    delivery_status: PreviewPrFeedbackNotificationDeliveryStatus,
    action: str,
    error_message: str = "",
) -> PreviewPrFeedbackNotificationAttemptRecord:
    attempt = PreviewPrFeedbackNotificationAttemptRecord(
        attempt_id=build_preview_pr_feedback_notification_attempt_id(
            feedback_id=feedback.feedback_id,
            event=event,
            policy_id=policy.policy_id,
            destination_id=destination.destination_id,
        ),
        feedback_id=feedback.feedback_id,
        event=event,
        policy_id=policy.policy_id,
        destination_id=destination.destination_id,
        destination_kind=destination.kind,
        delivery_status=delivery_status,
        attempted_at=attempted_at,
        action=action,
        error_message=bounded_text(error_message, max_length=500),
    )
    notification_store.write_preview_pr_feedback_notification_attempt_record(attempt)
    return attempt


def preview_pr_feedback_discord_payload(
    feedback: PreviewPrFeedbackRecord,
    *,
    event: PreviewPrFeedbackNotificationEvent,
) -> dict[str, object]:
    fields = [
        {"name": "Product", "value": feedback.product, "inline": True},
        {"name": "Context", "value": feedback.context, "inline": True},
        {"name": "Repository", "value": feedback.repository, "inline": True},
        {"name": "PR", "value": str(feedback.anchor_pr_number), "inline": True},
        {"name": "Delivery", "value": feedback.delivery_status, "inline": True},
        {"name": "Status", "value": feedback.status, "inline": True},
        {"name": "Feedback", "value": feedback.feedback_id, "inline": False},
    ]
    if feedback.preview_url:
        fields.append({"name": "Preview", "value": feedback.preview_url, "inline": False})
    if feedback.anchor_pr_url:
        fields.append({"name": "Pull request", "value": feedback.anchor_pr_url, "inline": False})
    if feedback.run_url:
        fields.append({"name": "Workflow", "value": feedback.run_url, "inline": False})
    description = feedback.error_message or feedback.failure_summary or feedback.comment_markdown
    return {
        "embeds": [
            {
                "title": "Launchplane preview PR feedback delivery failed",
                "description": description,
                "color": 0xC62828,
                "fields": fields,
                "footer": {"text": event},
            }
        ]
    }


def launchplane_managed_secret_resolver(
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


def bounded_text(value: str, *, max_length: int) -> str:
    normalized_value = " ".join(value.strip().split())
    if len(normalized_value) <= max_length:
        return normalized_value
    return f"{normalized_value[: max_length - 3]}..."
