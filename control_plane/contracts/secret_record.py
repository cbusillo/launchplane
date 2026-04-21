from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SecretScope = Literal["global", "context", "context_instance"]
SecretPolicy = Literal["write_only"]
SecretStatus = Literal["configured", "disabled"]
SecretEventType = Literal["created", "rotated", "imported", "validated", "disabled"]


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
