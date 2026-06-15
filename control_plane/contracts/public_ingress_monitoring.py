from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_health_monitoring_migration import health_check_record_token
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus


PublicIngressObservationStatus = Literal["pass", "fail", "skipped"]
PublicIngressIncidentStatus = Literal["open", "resolved"]
PublicIngressIncidentEvent = Literal["opened", "updated", "resolved"]
PublicIngressNotificationDestinationKind = Literal["github_issue", "email", "discord"]
PublicIngressNotificationStatus = Literal["enabled", "disabled"]
PublicIngressNotificationDeliveryStatus = Literal["delivered", "skipped", "failed"]
PublicIngressHealthCheckKind = Literal["public_http", "private_http", "provider"]
PublicIngressTargetKind = Literal["base_url", "health_url", "private_health_url", "provider"]
PublicIngressFailureCode = Literal[
    "connection_timeout",
    "dns_failure",
    "health_status_error",
    "http_error",
    "invalid_url",
    "private_url",
    "provider_check_unavailable",
    "redirect_loop",
    "self_redirect",
    "tls_failure",
    "wrong_runtime_identity",
    "unknown_error",
]


class PublicIngressTargetObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PublicIngressTargetKind
    url: str
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    final_url: str = ""
    redirect_count: int = Field(default=0, ge=0)
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    observed_runtime_identity: RuntimeIdentity | None = None
    summary: str

    @model_validator(mode="after")
    def _validate_observation(self) -> "PublicIngressTargetObservation":
        self.url = _required_text(self.url, "public ingress target observation requires url")
        self.final_url = self.final_url.strip()
        self.summary = _required_text(
            self.summary, "public ingress target observation requires summary"
        )
        if self.status == "pass" and self.failure_code is not None:
            raise ValueError("passing public ingress target cannot include failure_code")
        if self.status == "fail" and self.failure_code is None:
            raise ValueError("failing public ingress target requires failure_code")
        if self.status == "skipped" and self.failure_code not in {None, "private_url"}:
            raise ValueError("skipped public ingress target can only use private_url failure_code")
        return self


class PublicIngressObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    product: str
    repository: str = ""
    driver_id: str = ""
    context: str
    instance: str
    check_name: str = "public-ingress"
    check_kind: PublicIngressHealthCheckKind = "public_http"
    observed_at: str
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None = None
    base_url: str = ""
    health_url: str = ""
    expected_runtime_identity: RuntimeIdentity | None = None
    targets: tuple[PublicIngressTargetObservation, ...] = ()
    notification_key: str = ""
    notification_sent: bool = False
    summary: str

    @model_validator(mode="after")
    def _validate_record(self) -> "PublicIngressObservationRecord":
        self.record_id = _required_text(
            self.record_id, "public ingress observation requires record_id"
        )
        self.product = _required_text(self.product, "public ingress observation requires product")
        self.context = _required_text(self.context, "public ingress observation requires context")
        self.instance = _required_text(
            self.instance, "public ingress observation requires instance"
        )
        self.observed_at = _required_text(
            self.observed_at, "public ingress observation requires observed_at"
        )
        self.repository = self.repository.strip()
        self.driver_id = self.driver_id.strip()
        self.check_name = _required_text(
            self.check_name, "public ingress observation requires check_name"
        )
        self.base_url = self.base_url.strip()
        self.health_url = self.health_url.strip()
        self.notification_key = self.notification_key.strip()
        self.summary = _required_text(self.summary, "public ingress observation requires summary")
        if self.status == "pass" and self.failure_code is not None:
            raise ValueError("passing public ingress observation cannot include failure_code")
        if self.status == "fail" and self.failure_code is None:
            raise ValueError("failing public ingress observation requires failure_code")
        if self.status == "skipped" and self.failure_code not in {None, "private_url"}:
            raise ValueError(
                "skipped public ingress observation can only use private_url failure_code"
            )
        if not self.targets:
            raise ValueError("public ingress observation requires at least one target")
        if self.status == "pass" and any(target.status != "pass" for target in self.targets):
            raise ValueError("passing public ingress observation requires all targets to pass")
        if self.status == "fail" and not any(target.status == "fail" for target in self.targets):
            raise ValueError("failing public ingress observation requires a failing target")
        return self


class PublicIngressIncidentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    incident_id: str
    product: str
    repository: str = ""
    driver_id: str = ""
    context: str
    instance: str
    check_name: str = "public-ingress"
    check_kind: PublicIngressHealthCheckKind = "public_http"
    status: PublicIngressIncidentStatus
    opened_at: str
    opened_observation_id: str
    latest_observation_id: str
    latest_observed_at: str
    failure_code: PublicIngressFailureCode
    resolved_at: str = ""
    resolved_observation_id: str = ""
    summary: str

    @model_validator(mode="after")
    def _validate_incident(self) -> "PublicIngressIncidentRecord":
        self.incident_id = _required_text(
            self.incident_id, "public ingress incident requires incident_id"
        )
        self.product = _required_text(self.product, "public ingress incident requires product")
        self.context = _required_text(self.context, "public ingress incident requires context")
        self.instance = _required_text(self.instance, "public ingress incident requires instance")
        self.opened_at = _required_text(
            self.opened_at, "public ingress incident requires opened_at"
        )
        self.opened_observation_id = _required_text(
            self.opened_observation_id,
            "public ingress incident requires opened_observation_id",
        )
        self.latest_observation_id = _required_text(
            self.latest_observation_id,
            "public ingress incident requires latest_observation_id",
        )
        self.latest_observed_at = _required_text(
            self.latest_observed_at,
            "public ingress incident requires latest_observed_at",
        )
        self.repository = self.repository.strip()
        self.driver_id = self.driver_id.strip()
        self.check_name = _required_text(
            self.check_name, "public ingress incident requires check_name"
        )
        self.resolved_at = self.resolved_at.strip()
        self.resolved_observation_id = self.resolved_observation_id.strip()
        self.summary = _required_text(self.summary, "public ingress incident requires summary")
        if self.status == "resolved":
            if not self.resolved_at:
                raise ValueError("resolved public ingress incident requires resolved_at")
            if not self.resolved_observation_id:
                raise ValueError(
                    "resolved public ingress incident requires resolved_observation_id"
                )
        if self.status == "open" and (self.resolved_at or self.resolved_observation_id):
            raise ValueError("open public ingress incident cannot include resolution fields")
        return self


class PublicIngressNotificationDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    kind: PublicIngressNotificationDestinationKind
    status: PublicIngressNotificationStatus = "enabled"
    github_repository: str = ""
    github_issue_number: int | None = Field(default=None, ge=1)
    github_label: str = ""
    email_to: tuple[str, ...] = ()
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username_secret: str = ""
    smtp_password_secret: str = ""
    discord_webhook_secret: str = ""

    @model_validator(mode="after")
    def _validate_destination(self) -> "PublicIngressNotificationDestination":
        self.destination_id = _required_text(
            self.destination_id, "public ingress notification destination requires destination_id"
        )
        self.github_repository = self.github_repository.strip()
        self.github_label = self.github_label.strip()
        self.email_from = self.email_from.strip()
        self.email_to = tuple(_normalize_optional_text(value) for value in self.email_to)
        if any(not value for value in self.email_to):
            raise ValueError("public ingress email notification recipients must be non-empty")
        self.smtp_host = self.smtp_host.strip()
        self.smtp_username_secret = self.smtp_username_secret.strip()
        self.smtp_password_secret = self.smtp_password_secret.strip()
        self.discord_webhook_secret = self.discord_webhook_secret.strip()
        if self.kind == "github_issue":
            if not self.github_repository or "/" not in self.github_repository:
                raise ValueError(
                    "public ingress GitHub notification destination requires owner/name repository"
                )
            if self.github_issue_number is None and not self.github_label:
                raise ValueError(
                    "public ingress GitHub notification destination requires issue number or label"
                )
        elif self.kind == "email":
            if not self.email_to:
                raise ValueError("public ingress email notification destination requires email_to")
            if not self.email_from:
                raise ValueError(
                    "public ingress email notification destination requires email_from"
                )
            if not self.smtp_host:
                raise ValueError("public ingress email notification destination requires smtp_host")
            if not self.smtp_username_secret:
                raise ValueError(
                    "public ingress email notification destination requires smtp_username_secret"
                )
            if not self.smtp_password_secret:
                raise ValueError(
                    "public ingress email notification destination requires smtp_password_secret"
                )
        elif self.kind == "discord":
            if not self.discord_webhook_secret:
                raise ValueError(
                    "public ingress Discord notification destination requires discord_webhook_secret"
                )
        return self


class PublicIngressNotificationPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    policy_id: str
    product: str = ""
    context: str = ""
    instance: str = ""
    check_name: str = ""
    check_kind: PublicIngressHealthCheckKind | Literal[""] = ""
    status: PublicIngressNotificationStatus = "enabled"
    destinations: tuple[PublicIngressNotificationDestination, ...] = ()
    created_at: str
    updated_at: str
    source: str = ""

    @model_validator(mode="after")
    def _validate_policy(self) -> "PublicIngressNotificationPolicyRecord":
        self.policy_id = _required_text(
            self.policy_id, "public ingress notification policy requires policy_id"
        )
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.check_name = self.check_name.strip()
        self.created_at = _required_text(
            self.created_at, "public ingress notification policy requires created_at"
        )
        self.updated_at = _required_text(
            self.updated_at, "public ingress notification policy requires updated_at"
        )
        self.source = self.source.strip()
        if not self.destinations:
            raise ValueError("public ingress notification policy requires destinations")
        destination_ids = [destination.destination_id for destination in self.destinations]
        if len(set(destination_ids)) != len(destination_ids):
            raise ValueError("public ingress notification destination ids must be unique")
        if self.instance and not self.context:
            raise ValueError(
                "public ingress notification policy with instance scope requires context"
            )
        if self.check_name and not self.instance:
            raise ValueError(
                "public ingress notification policy with check_name scope requires instance"
            )
        if self.check_kind and not (self.context and self.instance):
            raise ValueError(
                "public ingress notification policy with check_kind scope requires lane scope"
            )
        return self

    def matches(self, incident: PublicIngressIncidentRecord) -> bool:
        return (
            (not self.product or self.product == incident.product)
            and (not self.context or self.context == incident.context)
            and (not self.instance or self.instance == incident.instance)
            and (
                not self.check_name
                or canonical_health_check_record_token(self.check_name)
                == canonical_health_check_record_token(incident.check_name)
            )
            and (not self.check_kind or self.check_kind == incident.check_kind)
        )


class PublicIngressNotificationAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    attempt_id: str
    incident_id: str
    event: PublicIngressIncidentEvent
    policy_id: str
    destination_id: str
    destination_kind: PublicIngressNotificationDestinationKind
    delivery_status: PublicIngressNotificationDeliveryStatus
    attempted_at: str
    observation_id: str
    external_url: str = ""
    external_id: str = ""
    action: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_attempt(self) -> "PublicIngressNotificationAttemptRecord":
        self.attempt_id = _required_text(
            self.attempt_id, "public ingress notification attempt requires attempt_id"
        )
        self.incident_id = _required_text(
            self.incident_id, "public ingress notification attempt requires incident_id"
        )
        self.policy_id = _required_text(
            self.policy_id, "public ingress notification attempt requires policy_id"
        )
        self.destination_id = _required_text(
            self.destination_id, "public ingress notification attempt requires destination_id"
        )
        self.attempted_at = _required_text(
            self.attempted_at, "public ingress notification attempt requires attempted_at"
        )
        self.observation_id = _required_text(
            self.observation_id,
            "public ingress notification attempt requires observation_id",
        )
        self.external_url = self.external_url.strip()
        self.external_id = self.external_id.strip()
        self.action = self.action.strip()
        self.error_message = self.error_message.strip()
        if self.delivery_status == "failed" and not self.error_message:
            raise ValueError("failed public ingress notification attempt requires error_message")
        if self.delivery_status == "delivered" and not self.action:
            raise ValueError("delivered public ingress notification attempt requires action")
        return self


def build_public_ingress_observation_id(
    *, product: str, context: str, instance: str, observed_at: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in ("public-ingress", product, context, instance, check_token, observed_at)
        if _record_token(value)
    )


def build_public_ingress_incident_id(
    *, product: str, context: str, instance: str, opened_at: str
) -> str:
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-incident", product, context, instance, opened_at)
        if _record_token(value)
    )


def build_public_ingress_lane_incident_id(
    *, product: str, context: str, instance: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-incident", product, context, instance, check_token)
        if _record_token(value)
    )


def build_public_ingress_notification_attempt_id(
    *, incident_id: str, event: str, policy_id: str, destination_id: str, observation_id: str
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "incident_id": incident_id.strip(),
                "event": event.strip(),
                "policy_id": policy_id.strip(),
                "destination_id": destination_id.strip(),
                "observation_id": observation_id.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        _record_token(value)
        for value in (
            "public-ingress-notification",
            incident_id,
            event,
            policy_id,
            destination_id,
            digest,
        )
        if _record_token(value)
    )


def _record_token(value: str) -> str:
    return health_check_record_token(value)


def _check_record_token(check_name: str) -> str:
    # Preserve legacy public-ingress observation and incident ids for the default
    # public HTTP check while still separating every explicitly named non-default check.
    return canonical_health_check_record_token(check_name)


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _normalize_optional_text(value: str) -> str:
    return value.strip()
