from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DurableOperationIdentityType = Literal[
    "github_actions",
    "github_human",
    "terminal_agent",
    "local_operator",
    "local_admin",
]


class DurableOperationCallerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    identity_type: DurableOperationIdentityType
    subject: str = ""
    token_label: str = ""
    repository: str = ""
    repository_owner: str = ""
    repository_id: str = ""
    repository_owner_id: str = ""
    workflow_ref: str = ""
    job_workflow_ref: str = ""
    ref: str = ""
    ref_type: str = ""
    event_name: str = ""
    environment: str = ""
    sha: str = ""
    login: str = ""
    github_id: int = Field(default=0, ge=0)
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    role: Literal["", "read_only", "admin"] = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "DurableOperationCallerIdentity":
        for field_name in (
            "subject",
            "token_label",
            "repository",
            "repository_owner",
            "repository_id",
            "repository_owner_id",
            "workflow_ref",
            "job_workflow_ref",
            "ref",
            "ref_type",
            "event_name",
            "environment",
            "sha",
            "login",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        self.organizations = _normalized_values(self.organizations)
        self.teams = _normalized_values(self.teams)
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation caller identity schema version.")
        if self.identity_type == "github_actions":
            if not self.repository or not self.workflow_ref or not self.subject:
                raise ValueError(
                    "Durable GitHub Actions identity requires repository, workflow_ref, and subject."
                )
        elif self.identity_type == "github_human":
            if not self.login or self.github_id < 1 or self.role not in {"read_only", "admin"}:
                raise ValueError(
                    "Durable GitHub human identity requires login, github_id, and role."
                )
        elif not self.subject or not self.token_label:
            raise ValueError("Durable token identity requires subject and token_label.")
        return self


class DurableOperationAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: str
    product: str
    context: str
    instances: tuple[str, ...]
    managed_set_id: str
    managed_rule_id: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_schema_version: Literal[2]
    policy_sha256: str
    policy_source: str
    authorized_at: str
    caller: DurableOperationCallerIdentity

    @model_validator(mode="after")
    def _validate_authorization(self) -> "DurableOperationAuthorization":
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation authorization schema version.")
        for field_name in (
            "action",
            "product",
            "context",
            "managed_set_id",
            "managed_rule_id",
            "policy_record_id",
            "policy_sha256",
            "policy_source",
            "authorized_at",
        ):
            normalized = str(getattr(self, field_name)).strip()
            if not normalized:
                raise ValueError(f"Durable operation authorization requires {field_name}.")
            setattr(self, field_name, normalized)
        self.context = self.context.lower()
        self.instances = tuple(value.lower() for value in _normalized_values(self.instances))
        if not self.instances:
            raise ValueError("Durable operation authorization requires exact instances.")
        if re.fullmatch(r"[0-9a-f]{64}", self.policy_sha256) is None:
            raise ValueError("Durable operation authorization requires a policy SHA-256.")
        return self


class DurableOperationCancellation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    reason: str
    cancelled_at: str
    caller: DurableOperationCallerIdentity

    @model_validator(mode="after")
    def _validate_cancellation(self) -> "DurableOperationCancellation":
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation cancellation schema version.")
        self.reason = self.reason.strip()
        self.cancelled_at = self.cancelled_at.strip()
        if not self.reason:
            raise ValueError("Durable operation cancellation requires reason.")
        if not self.cancelled_at:
            raise ValueError("Durable operation cancellation requires cancelled_at.")
        return self


class DurableOperationCancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "DurableOperationCancellationRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Durable operation cancellation requires reason.")
        return self


def _normalized_values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
