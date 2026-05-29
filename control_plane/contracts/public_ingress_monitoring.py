from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus


PublicIngressObservationStatus = Literal["pass", "fail", "skipped"]
PublicIngressTargetKind = Literal["base_url", "health_url"]
PublicIngressFailureCode = Literal[
    "connection_timeout",
    "dns_failure",
    "health_status_error",
    "http_error",
    "invalid_url",
    "private_url",
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


def build_public_ingress_observation_id(
    *, product: str, context: str, instance: str, observed_at: str
) -> str:
    return "-".join(
        _record_token(value)
        for value in ("public-ingress", product, context, instance, observed_at)
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
