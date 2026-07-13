from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


OutboxDeliveryKind = Literal[
    "github_workflow_dispatch",
    "public_ingress_notification",
]
OutboxDeliveryState = Literal[
    "pending",
    "running",
    "delivered",
    "failed",
    "reconcile_required",
]


class OutboxDeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    delivery_id: str
    kind: OutboxDeliveryKind
    state: OutboxDeliveryState = "pending"
    aggregate_type: str
    aggregate_id: str
    dedupe_key: str
    created_at: str
    updated_at: str
    next_attempt_at: str
    lease_owner: str = ""
    lease_expires_at: str = ""
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=6, ge=1, le=100)
    provider_operation_key: str = ""
    provider_id: str = ""
    external_id: str = ""
    external_url: str = ""
    action: str = ""
    error_code: str = ""
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record(self) -> "OutboxDeliveryRecord":
        self.delivery_id = _required_text(self.delivery_id, "outbox delivery requires delivery_id")
        self.aggregate_type = _required_text(
            self.aggregate_type, "outbox delivery requires aggregate_type"
        )
        self.aggregate_id = _required_text(
            self.aggregate_id, "outbox delivery requires aggregate_id"
        )
        self.dedupe_key = _required_text(self.dedupe_key, "outbox delivery requires dedupe_key")
        self.created_at = _required_text(self.created_at, "outbox delivery requires created_at")
        self.updated_at = _required_text(self.updated_at, "outbox delivery requires updated_at")
        self.next_attempt_at = _required_text(
            self.next_attempt_at, "outbox delivery requires next_attempt_at"
        )
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.provider_operation_key = self.provider_operation_key.strip()
        self.provider_id = self.provider_id.strip()
        self.external_id = self.external_id.strip()
        self.external_url = self.external_url.strip()
        self.action = self.action.strip()
        self.error_code = self.error_code.strip()
        if self.state == "running":
            if not self.lease_owner:
                raise ValueError("running outbox deliveries require lease_owner")
            if not self.lease_expires_at:
                raise ValueError("running outbox deliveries require lease_expires_at")
        elif self.lease_owner or self.lease_expires_at:
            raise ValueError("non-running outbox deliveries cannot retain lease fields")
        if self.state == "delivered" and not self.action:
            raise ValueError("delivered outbox deliveries require action")
        if self.state == "failed" and not self.error_code:
            raise ValueError("failed outbox deliveries require error_code")
        if self.state == "reconcile_required" and not self.provider_operation_key:
            raise ValueError("reconcile-required outbox deliveries require provider_operation_key")
        _reject_sensitive_payload(self.payload)
        return self


def build_outbox_delivery_id(*, kind: str, dedupe_key: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"kind": kind.strip(), "dedupe_key": dedupe_key.strip()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return "-".join(
        token
        for token in (
            "outbox",
            _record_token(kind),
            digest,
        )
        if token
    )


def build_outbox_dedupe_key(*, kind: str, parts: tuple[str, ...]) -> str:
    return ":".join([kind.strip(), *(part.strip() for part in parts if part.strip())])


def retry_outbox_delivery(
    record: OutboxDeliveryRecord,
    *,
    error_code: str,
) -> OutboxDeliveryRecord:
    return record.model_copy(
        update={
            "state": "pending",
            "error_code": error_code.strip() or "provider_error",
            "action": "",
            "external_id": "",
            "external_url": "",
            "lease_owner": "",
            "lease_expires_at": "",
        }
    )


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _record_token(value: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in value.strip().lower()
    ).strip("-")


def _reject_sensitive_payload(value: object, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if _looks_sensitive_key(key):
                raise ValueError(
                    f"outbox delivery payload cannot include sensitive field {path}.{key}"
                )
            _reject_sensitive_payload(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_payload(nested, path=f"{path}[{index}]")


def _looks_sensitive_key(key: str) -> bool:
    sensitive_tokens = (
        "authorization",
        "bearer",
        "ciphertext",
        "cookie",
        "password",
        "secret",
        "token",
        "webhook_url",
    )
    return any(token in key for token in sensitive_tokens)
