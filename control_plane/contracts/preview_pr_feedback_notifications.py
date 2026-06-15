from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord


PreviewPrFeedbackNotificationDestinationKind = Literal["discord"]
PreviewPrFeedbackNotificationStatus = Literal["enabled", "disabled"]
PreviewPrFeedbackNotificationEvent = Literal["delivery_skipped", "delivery_failed"]
PreviewPrFeedbackNotificationDeliveryStatus = Literal[
    "pending",
    "delivered",
    "failed",
    "skipped",
]


class PreviewPrFeedbackNotificationDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    kind: PreviewPrFeedbackNotificationDestinationKind
    status: PreviewPrFeedbackNotificationStatus = "enabled"
    discord_webhook_secret: str = ""

    @model_validator(mode="after")
    def _validate_destination(self) -> "PreviewPrFeedbackNotificationDestination":
        self.destination_id = _required_text(
            self.destination_id,
            "preview PR feedback notification destination requires destination_id",
        )
        self.discord_webhook_secret = self.discord_webhook_secret.strip()
        if self.kind == "discord" and not self.discord_webhook_secret:
            raise ValueError(
                "preview PR feedback Discord notification destination requires"
                " discord_webhook_secret"
            )
        return self


class PreviewPrFeedbackNotificationPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    policy_id: str
    product: str = ""
    context: str = ""
    repository: str = ""
    status: PreviewPrFeedbackNotificationStatus = "enabled"
    destinations: tuple[PreviewPrFeedbackNotificationDestination, ...] = ()
    created_at: str
    updated_at: str
    source: str = ""

    @model_validator(mode="after")
    def _validate_policy(self) -> "PreviewPrFeedbackNotificationPolicyRecord":
        self.policy_id = _required_text(
            self.policy_id, "preview PR feedback notification policy requires policy_id"
        )
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.repository = self.repository.strip()
        self.created_at = _required_text(
            self.created_at,
            "preview PR feedback notification policy requires created_at",
        )
        self.updated_at = _required_text(
            self.updated_at,
            "preview PR feedback notification policy requires updated_at",
        )
        self.source = self.source.strip()
        if self.repository and "/" not in self.repository:
            raise ValueError(
                "preview PR feedback notification policy repository must be owner/name"
            )
        if not self.destinations:
            raise ValueError("preview PR feedback notification policy requires destinations")
        destination_ids = [destination.destination_id for destination in self.destinations]
        if len(set(destination_ids)) != len(destination_ids):
            raise ValueError("preview PR feedback notification destination ids must be unique")
        return self

    def matches(self, feedback: PreviewPrFeedbackRecord) -> bool:
        return (
            (not self.product or self.product == feedback.product)
            and (not self.context or self.context == feedback.context)
            and (not self.repository or self.repository == feedback.repository)
        )


class PreviewPrFeedbackNotificationAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    attempt_id: str
    feedback_id: str
    event: PreviewPrFeedbackNotificationEvent
    policy_id: str
    destination_id: str
    destination_kind: PreviewPrFeedbackNotificationDestinationKind
    delivery_status: PreviewPrFeedbackNotificationDeliveryStatus
    attempted_at: str
    action: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_attempt(self) -> "PreviewPrFeedbackNotificationAttemptRecord":
        self.attempt_id = _required_text(
            self.attempt_id, "preview PR feedback notification attempt requires attempt_id"
        )
        self.feedback_id = _required_text(
            self.feedback_id, "preview PR feedback notification attempt requires feedback_id"
        )
        self.policy_id = _required_text(
            self.policy_id, "preview PR feedback notification attempt requires policy_id"
        )
        self.destination_id = _required_text(
            self.destination_id,
            "preview PR feedback notification attempt requires destination_id",
        )
        self.attempted_at = _required_text(
            self.attempted_at,
            "preview PR feedback notification attempt requires attempted_at",
        )
        self.action = self.action.strip()
        self.error_message = self.error_message.strip()
        if self.delivery_status == "failed" and not self.error_message:
            raise ValueError(
                "failed preview PR feedback notification attempt requires error_message"
            )
        if self.delivery_status == "delivered" and not self.action:
            raise ValueError("delivered preview PR feedback notification attempt requires action")
        if self.delivery_status == "pending" and self.error_message:
            raise ValueError(
                "pending preview PR feedback notification attempt cannot include error_message"
            )
        return self


def preview_pr_feedback_notification_event(
    feedback: PreviewPrFeedbackRecord,
) -> PreviewPrFeedbackNotificationEvent:
    if feedback.delivery_status == "failed":
        return "delivery_failed"
    return "delivery_skipped"


def build_preview_pr_feedback_notification_attempt_id(
    *,
    feedback_id: str,
    event: str,
    policy_id: str,
    destination_id: str,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "feedback_id": feedback_id.strip(),
                "event": event.strip(),
                "policy_id": policy_id.strip(),
                "destination_id": destination_id.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        _record_token(value)
        for value in (
            "preview-pr-feedback-notification",
            feedback_id,
            event,
            policy_id,
            destination_id,
            digest,
        )
        if _record_token(value)
    )


def _record_token(value: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in value.strip().lower()
    ).strip("-")


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
