from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


DurableOperationIdentityType = Literal[
    "github_actions",
    "github_human",
    "terminal_agent",
    "local_operator",
    "local_admin",
    "launchplane_reconcile",
]
# "policy_rule": a caller's managed authz rule, re-checked against the active policy.
# "launchplane_reconcile": Launchplane's own reconciler; no caller and no policy rule.
# "policy_administrator": the signed-in person the active policy names as its
# administrator by immutable GitHub id; re-checked against the active policy.
# "client_release_acceptance": the product's recorded Client accepted the release;
# no policy rule. It names the decision, and the worker re-checks that the decision
# is still the product's newest, accepted, by its Client, and that releases are not held.
DurableOperationGrant = Literal[
    "policy_rule", "launchplane_reconcile", "policy_administrator", "client_release_acceptance"
]
LAUNCHPLANE_RECONCILE_SUBJECT = "launchplane-reconciler"
_MANAGED_RULE_TEXT_FIELDS = ("managed_set_id", "managed_rule_id")
_POLICY_PROVENANCE_TEXT_FIELDS = ("policy_record_id", "policy_sha256", "policy_source")
_POLICY_RULE_TEXT_FIELDS = (*_MANAGED_RULE_TEXT_FIELDS, *_POLICY_PROVENANCE_TEXT_FIELDS)
_CALLER_TEXT_FIELDS = (
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
)


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
    # "client": the product's Client, who holds no policy role.
    role: Literal["", "read_only", "admin", "client"] = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "DurableOperationCallerIdentity":
        for field_name in _CALLER_TEXT_FIELDS:
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        self.organizations = _normalized_values(self.organizations)
        self.teams = _normalized_values(self.teams)
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation caller identity schema version.")
        if self.identity_type == "launchplane_reconcile":
            other_values = (
                *(getattr(self, name) for name in _CALLER_TEXT_FIELDS if name != "subject"),
                self.github_id,
                self.organizations,
                self.teams,
                self.role,
            )
            if self.subject != LAUNCHPLANE_RECONCILE_SUBJECT or any(other_values):
                raise ValueError("Launchplane reconcile identity carries only its fixed subject.")
        elif self.identity_type == "github_actions":
            if not self.repository or not self.workflow_ref or not self.subject:
                raise ValueError(
                    "Durable GitHub Actions identity requires repository, workflow_ref, and subject."
                )
        elif self.identity_type == "github_human":
            if not self.login or self.github_id < 1 or not self.role:
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
    managed_set_id: str = ""
    managed_rule_id: str = ""
    policy_record_id: str = ""
    policy_revision: int = Field(default=0, ge=0)
    policy_schema_version: Literal[2, 3] | None = None
    policy_sha256: str = ""
    policy_source: str = ""
    authorized_at: str
    caller: DurableOperationCallerIdentity
    grant: DurableOperationGrant = "policy_rule"
    release_decision_record_id: str = ""

    @model_validator(mode="after")
    def _validate_authorization(self) -> "DurableOperationAuthorization":
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation authorization schema version.")
        for field_name in ("action", "product", "context", "authorized_at"):
            normalized = str(getattr(self, field_name)).strip()
            if not normalized:
                raise ValueError(f"Durable operation authorization requires {field_name}.")
            setattr(self, field_name, normalized)
        for field_name in _POLICY_RULE_TEXT_FIELDS:
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        self.context = self.context.lower()
        self.instances = tuple(value.lower() for value in _normalized_values(self.instances))
        self.release_decision_record_id = self.release_decision_record_id.strip()
        if not self.instances:
            raise ValueError("Durable operation authorization requires exact instances.")
        if (self.grant == "client_release_acceptance") != bool(self.release_decision_record_id):
            raise ValueError("Only a Client release grant names a release decision, and it must.")
        if (self.grant == "client_release_acceptance") != (self.caller.role == "client"):
            raise ValueError("Only a Client release grant has a Client caller.")
        if self.grant == "client_release_acceptance":
            if self.caller.identity_type != "github_human":
                raise ValueError("A Client release grant requires a GitHub human caller.")
            if (
                any(getattr(self, field_name) for field_name in _POLICY_RULE_TEXT_FIELDS)
                or self.policy_revision
                or self.policy_schema_version is not None
            ):
                raise ValueError("A Client release grant carries no policy rule.")
            return self
        if self.grant == "launchplane_reconcile":
            if self.caller.identity_type != "launchplane_reconcile":
                raise ValueError("A Launchplane reconcile grant requires the reconcile identity.")
            if (
                any(getattr(self, field_name) for field_name in _POLICY_RULE_TEXT_FIELDS)
                or self.policy_revision
                or self.policy_schema_version is not None
            ):
                raise ValueError("A Launchplane reconcile grant carries no policy rule.")
            return self
        if self.caller.identity_type == "launchplane_reconcile":
            raise ValueError("The Launchplane reconcile identity has no policy-rule grant.")
        required_text_fields: tuple[str, ...] = _POLICY_RULE_TEXT_FIELDS
        if self.grant == "policy_administrator":
            if self.caller.identity_type != "github_human" or self.caller.role != "admin":
                raise ValueError("An admin grant requires a GitHub human caller with role admin.")
            if any(getattr(self, field_name) for field_name in _MANAGED_RULE_TEXT_FIELDS):
                raise ValueError("An admin grant carries no managed rule.")
            required_text_fields = _POLICY_PROVENANCE_TEXT_FIELDS
        for field_name in required_text_fields:
            if not getattr(self, field_name):
                raise ValueError(f"Durable operation authorization requires {field_name}.")
        if self.policy_revision < 1:
            raise ValueError("Durable operation authorization requires policy_revision.")
        if self.policy_schema_version is None:
            raise ValueError("Durable operation authorization requires policy_schema_version.")
        if re.fullmatch(r"[0-9a-f]{64}", self.policy_sha256) is None:
            raise ValueError("Durable operation authorization requires a policy SHA-256.")
        return self

    @model_serializer(mode="wrap")
    def _serialize_authorization(self, handler: SerializerFunctionWrapHandler) -> object:
        # A policy-rule grant serializes exactly as it did before the grant field existed;
        # the other grants omit the fields they never carry.
        payload = handler(self)
        if isinstance(payload, dict):
            if self.grant != "client_release_acceptance":
                payload.pop("release_decision_record_id", None)
            if self.grant == "policy_rule":
                payload.pop("grant", None)
            elif self.grant == "policy_administrator":
                for field_name in _MANAGED_RULE_TEXT_FIELDS:
                    payload.pop(field_name, None)
            else:
                for field_name in (
                    *_POLICY_RULE_TEXT_FIELDS,
                    "policy_revision",
                    "policy_schema_version",
                ):
                    payload.pop(field_name, None)
        return payload


class DurableOperationReconciliationAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    provider_inspected_at: str
    provider_state: str
    evidence_reference: str
    safe_to_release: Literal[True]

    @model_validator(mode="after")
    def _validate_attestation(self) -> "DurableOperationReconciliationAttestation":
        if self.schema_version != 1:
            raise ValueError("Unsupported durable operation reconciliation schema version.")
        self.provider_inspected_at = _normalize_utc_timestamp(self.provider_inspected_at)
        for field_name in ("provider_state", "evidence_reference"):
            normalized = str(getattr(self, field_name)).strip()
            if not normalized:
                raise ValueError(f"Durable operation reconciliation requires {field_name}.")
            setattr(self, field_name, normalized)
        return self


class DurableOperationCancellation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    reason: str
    cancelled_at: str
    caller: DurableOperationCallerIdentity
    reconciliation_attestation: DurableOperationReconciliationAttestation | None = None

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
    reconciliation_attestation: DurableOperationReconciliationAttestation | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "DurableOperationCancellationRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Durable operation cancellation requires reason.")
        return self


def _normalized_values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _normalize_utc_timestamp(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "Durable operation reconciliation provider_inspected_at must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "Durable operation reconciliation provider_inspected_at requires a timezone."
        )
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
