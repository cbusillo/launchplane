"""Value-free original recipient identity, independent of current lane records."""

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


def requires_provider_target_origin(target_key: str) -> bool:
    # Rebuild effects append a phase to a canonical physical key. They retain
    # their separate operation fences, without becoming shared target holders.
    child_key = re.fullmatch(r"generic-web-provider-target:[0-9a-f]{64}:.+", target_key)
    return target_key.startswith("generic-web-provider-target:") and child_key is None


class ProviderTargetOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    provider_id: str
    target_type: str
    target_id: str

    @field_validator("product", "context", "instance", "provider_id", "target_type", "target_id")
    @classmethod
    def required_identity(cls, value: str) -> str:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Original provider recipient requires exact nonempty identity.")
        return value

    def target_key(self) -> str:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )

        return generic_web_provider_target_key(
            provider_id=self.provider_id,
            context=self.context,
            instance=self.instance,
            target_type=self.target_type,
            target_id=self.target_id,
        )


class RetainedProviderTargetOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: ProviderTargetOrigin
    operation_nonce: UUID
