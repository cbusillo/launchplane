from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord


EveryCodeNotificationDestinationKind = Literal["discord"]
EveryCodeNotificationStatus = Literal["enabled", "disabled"]
EveryCodeNotificationEvent = Literal["work_request_blocked"]
EveryCodeNotificationDeliveryStatus = Literal["pending", "delivered", "failed", "skipped"]


class EveryCodeNotificationDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    kind: EveryCodeNotificationDestinationKind
    status: EveryCodeNotificationStatus = "enabled"
    discord_webhook_secret: str = ""

    @model_validator(mode="after")
    def _validate_destination(self) -> "EveryCodeNotificationDestination":
        self.destination_id = _required_text(
            self.destination_id, "Every Code notification destination requires destination_id"
        )
        self.discord_webhook_secret = self.discord_webhook_secret.strip()
        if self.kind == "discord" and not self.discord_webhook_secret:
            raise ValueError(
                "Every Code Discord notification destination requires discord_webhook_secret"
            )
        return self


class EveryCodeNotificationPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    policy_id: str
    repository: str = ""
    status: EveryCodeNotificationStatus = "enabled"
    destinations: tuple[EveryCodeNotificationDestination, ...] = ()
    created_at: str
    updated_at: str
    source: str = ""

    @model_validator(mode="after")
    def _validate_policy(self) -> "EveryCodeNotificationPolicyRecord":
        self.policy_id = _required_text(
            self.policy_id, "Every Code notification policy requires policy_id"
        )
        self.repository = self.repository.strip()
        self.created_at = _required_text(
            self.created_at, "Every Code notification policy requires created_at"
        )
        self.updated_at = _required_text(
            self.updated_at, "Every Code notification policy requires updated_at"
        )
        self.source = self.source.strip()
        if self.repository and "/" not in self.repository:
            raise ValueError(
                "Every Code notification policy repository must be owner/name when set"
            )
        if not self.destinations:
            raise ValueError("Every Code notification policy requires destinations")
        destination_ids = [destination.destination_id for destination in self.destinations]
        if len(set(destination_ids)) != len(destination_ids):
            raise ValueError("Every Code notification destination ids must be unique")
        return self

    def matches(self, request: EveryCodeWorkRequestRecord) -> bool:
        return not self.repository or self.repository == request.repository


class EveryCodeNotificationAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    attempt_id: str
    request_id: str
    event: EveryCodeNotificationEvent
    policy_id: str
    destination_id: str
    destination_kind: EveryCodeNotificationDestinationKind
    delivery_status: EveryCodeNotificationDeliveryStatus
    attempted_at: str
    action: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_attempt(self) -> "EveryCodeNotificationAttemptRecord":
        self.attempt_id = _required_text(
            self.attempt_id, "Every Code notification attempt requires attempt_id"
        )
        self.request_id = _required_text(
            self.request_id, "Every Code notification attempt requires request_id"
        )
        self.policy_id = _required_text(
            self.policy_id, "Every Code notification attempt requires policy_id"
        )
        self.destination_id = _required_text(
            self.destination_id, "Every Code notification attempt requires destination_id"
        )
        self.attempted_at = _required_text(
            self.attempted_at, "Every Code notification attempt requires attempted_at"
        )
        self.action = self.action.strip()
        self.error_message = self.error_message.strip()
        if self.delivery_status == "failed" and not self.error_message:
            raise ValueError("failed Every Code notification attempt requires error_message")
        if self.delivery_status == "delivered" and not self.action:
            raise ValueError("delivered Every Code notification attempt requires action")
        if self.delivery_status == "pending" and self.error_message:
            raise ValueError("pending Every Code notification attempt cannot include error_message")
        return self


def build_every_code_notification_attempt_id(
    *,
    request_id: str,
    event: str,
    policy_id: str,
    destination_id: str,
    lifecycle_key: str = "",
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "request_id": request_id.strip(),
                "event": event.strip(),
                "policy_id": policy_id.strip(),
                "destination_id": destination_id.strip(),
                "lifecycle_key": lifecycle_key.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        _record_token(value)
        for value in (
            "every-code-notification",
            request_id,
            event,
            policy_id,
            destination_id,
            lifecycle_key,
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
