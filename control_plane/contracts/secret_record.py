from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.dokploy_target_record import IntegrationAllowanceKind
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretClass

SecretScope = Literal["global", "context", "context_instance"]
SecretPolicy = Literal["write_only"]
SecretStatus = Literal["configured", "disabled"]
SecretEventType = Literal["created", "rotated", "imported", "validated", "disabled", "relabelled"]
SecretSharingKind = IntegrationAllowanceKind | Literal["site_shared"]


class SecretRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    secret_id: str
    scope: SecretScope
    integration: str
    name: str
    context: str = ""
    instance: str = ""
    description: str = ""
    policy: SecretPolicy = "write_only"
    status: SecretStatus = "configured"
    current_version_id: str
    created_at: str
    updated_at: str
    last_validated_at: str = ""
    updated_by: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "SecretRecord":
        if not self.secret_id.strip():
            raise ValueError("secret record requires secret_id")
        if not self.integration.strip():
            raise ValueError("secret record requires integration")
        if not self.name.strip():
            raise ValueError("secret record requires name")
        if not self.current_version_id.strip():
            raise ValueError("secret record requires current_version_id")
        if not self.created_at.strip() or not self.updated_at.strip():
            raise ValueError("secret record requires created_at and updated_at")
        if self.scope in {"context", "context_instance"} and not self.context.strip():
            raise ValueError("context-scoped secret record requires context")
        if self.scope == "context_instance" and not self.instance.strip():
            raise ValueError("instance-scoped secret record requires instance")
        if self.scope != "context_instance" and self.instance.strip():
            raise ValueError("only instance-scoped secret records may set instance")
        return self


class SecretVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    version_id: str
    secret_id: str
    created_at: str
    created_by: str = ""
    cipher_alg: Literal["fernet-v1"] = "fernet-v1"
    key_id: str = "launchplane-master-key"
    ciphertext: str

    @model_validator(mode="after")
    def _validate_record(self) -> "SecretVersion":
        if not self.version_id.strip():
            raise ValueError("secret version requires version_id")
        if not self.secret_id.strip():
            raise ValueError("secret version requires secret_id")
        if not self.created_at.strip():
            raise ValueError("secret version requires created_at")
        if not self.ciphertext.strip():
            raise ValueError("secret version requires ciphertext")
        return self


class SecretSharingReason(BaseModel):
    """Why a production integration key may sit on one non-production lane.

    The kinds are the lane integration allowances' (``dev_store``,
    ``read_only_source``, ``pre_live``) plus ``site_shared`` for a key the site's
    stable lanes share on purpose. ``evidence`` says who verified the key's
    permissions, when, and what they saw. Launchplane cannot check a token's
    permissions; a person does and records it here.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SecretSharingKind
    reason: str
    evidence: str
    recorded_by: str = ""
    recorded_at: str = ""

    @model_validator(mode="after")
    def _validate_reason(self) -> "SecretSharingReason":
        self.reason = self.reason.strip()
        self.evidence = self.evidence.strip()
        self.recorded_by = self.recorded_by.strip()
        self.recorded_at = self.recorded_at.strip()
        if not self.reason:
            raise ValueError("A secret sharing reason requires reason.")
        if not self.evidence:
            raise ValueError(
                "A secret sharing reason requires evidence: who verified the key, when, and what."
            )
        return self


class SecretBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    binding_id: str
    secret_id: str
    integration: str
    binding_type: Literal["env"] = "env"
    binding_key: str
    context: str = ""
    instance: str = ""
    status: SecretStatus = "configured"
    # The writer's key-safety classification for a secret stored on one exact
    # lane. Runtime key safety uses it only for that lane's own bindings.
    declared_secret_class: RuntimeSecretClass | None = None
    # Why the declared class is safe, for a production integration key shared
    # with a non-production lane. Metadata only; never part of the value.
    sharing_reason: SecretSharingReason | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _validate_record(self) -> "SecretBinding":
        if not self.binding_id.strip():
            raise ValueError("secret binding requires binding_id")
        if not self.secret_id.strip():
            raise ValueError("secret binding requires secret_id")
        if not self.integration.strip():
            raise ValueError("secret binding requires integration")
        if not self.binding_key.strip():
            raise ValueError("secret binding requires binding_key")
        if not self.created_at.strip() or not self.updated_at.strip():
            raise ValueError("secret binding requires created_at and updated_at")
        if self.declared_secret_class is not None and not (
            self.context.strip() and self.instance.strip()
        ):
            raise ValueError("secret binding declared_secret_class requires context and instance")
        if self.sharing_reason is not None and self.declared_secret_class is None:
            raise ValueError("secret binding sharing_reason requires declared_secret_class")
        return self


class SecretAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_id: str
    secret_id: str
    event_type: SecretEventType
    recorded_at: str
    actor: str = ""
    detail: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record(self) -> "SecretAuditEvent":
        if not self.event_id.strip():
            raise ValueError("secret audit event requires event_id")
        if not self.secret_id.strip():
            raise ValueError("secret audit event requires secret_id")
        if not self.recorded_at.strip():
            raise ValueError("secret audit event requires recorded_at")
        return self


class SecretRotationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version_id: str
    record: SecretRecord
    version: SecretVersion
    audit_event: SecretAuditEvent

    @model_validator(mode="after")
    def _validate_record(self) -> "SecretRotationWrite":
        if not self.expected_current_version_id.strip():
            raise ValueError("secret rotation write requires expected_current_version_id")
        if self.record.secret_id != self.version.secret_id:
            raise ValueError("secret rotation record and version must reference the same secret")
        if self.record.secret_id != self.audit_event.secret_id:
            raise ValueError(
                "secret rotation record and audit event must reference the same secret"
            )
        if self.record.current_version_id != self.version.version_id:
            raise ValueError("secret rotation record must point to the new version")
        if self.expected_current_version_id == self.version.version_id:
            raise ValueError("secret rotation must create a new version")
        return self
