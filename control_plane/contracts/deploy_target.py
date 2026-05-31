from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DeployTargetCategory = Literal[
    "application",
    "compose",
    "container",
    "service",
    "static",
    "unknown",
]


class DeployedTargetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    target_category: DeployTargetCategory = "unknown"
    target_id: str
    display_name: str
    provider_target_type: str = ""
    provider_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_target(self) -> "DeployedTargetReference":
        self.provider_id = self.provider_id.strip().lower()
        self.target_id = self.target_id.strip()
        self.display_name = self.display_name.strip()
        self.provider_target_type = self.provider_target_type.strip().lower()
        if not self.provider_id:
            raise ValueError("deployed target reference requires provider_id")
        if not self.target_id:
            raise ValueError("deployed target reference requires target_id")
        if not self.display_name:
            raise ValueError("deployed target reference requires display_name")
        normalized_evidence: dict[str, str] = {}
        for raw_key, raw_value in self.provider_evidence.items():
            key = raw_key.strip()
            if not key:
                raise ValueError("deployed target provider evidence keys must be non-empty")
            normalized_evidence[key] = raw_value.strip()
        self.provider_evidence = normalized_evidence
        return self
