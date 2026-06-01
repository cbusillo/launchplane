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

DeployTargetCompatibilityType = Literal["application", "compose"]


class DeployTargetContractReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_name: str
    provider_id: str = "dokploy"
    target_category: DeployTargetCategory = "unknown"
    provider_target_type: str = ""

    @model_validator(mode="after")
    def _validate_target(self) -> "DeployTargetContractReference":
        self.target_name = self.target_name.strip()
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        if not self.target_name:
            raise ValueError("deploy target contract reference requires target_name")
        if not self.provider_id:
            raise ValueError("deploy target contract reference requires provider_id")
        return self

    def legacy_target_type(self) -> DeployTargetCompatibilityType | None:
        if self.target_category == "application":
            return "application"
        if self.target_category == "compose":
            return "compose"
        if self.provider_target_type == "application":
            return "application"
        if self.provider_target_type == "compose":
            return "compose"
        return None


def apply_target_reference_defaults(data: object) -> object:
    if not isinstance(data, dict):
        return data
    raw_reference = data.get("target_reference")
    if raw_reference is None:
        return data
    reference = DeployTargetContractReference.model_validate(raw_reference)
    updated = dict(data)
    if not str(updated.get("target_name", "")).strip():
        updated["target_name"] = reference.target_name
    if not str(updated.get("provider_id", "")).strip():
        updated["provider_id"] = reference.provider_id
    if updated.get("target_category") in {None, ""}:
        updated["target_category"] = reference.target_category
    if not str(updated.get("provider_target_type", "")).strip():
        updated["provider_target_type"] = reference.provider_target_type
    if not str(updated.get("target_type", "")).strip():
        legacy_target_type = reference.legacy_target_type()
        if legacy_target_type is not None:
            updated["target_type"] = legacy_target_type
    return updated


def ensure_target_reference_matches(
    reference: DeployTargetContractReference | None,
    *,
    target_name: str,
    target_type: DeployTargetCompatibilityType,
    provider_id: str,
    target_category: DeployTargetCategory,
    provider_target_type: str,
) -> DeployTargetContractReference:
    if target_category == "application" and target_type != "application":
        raise ValueError("target_category does not match target_type")
    if target_category == "compose" and target_type != "compose":
        raise ValueError("target_category does not match target_type")
    if provider_id == "dokploy" and provider_target_type != target_type:
        raise ValueError("provider_target_type does not match target_type")
    canonical_reference = DeployTargetContractReference(
        target_name=target_name,
        provider_id=provider_id,
        target_category=target_category,
        provider_target_type=provider_target_type,
    )
    if reference is None:
        return canonical_reference
    if reference.target_name != canonical_reference.target_name:
        raise ValueError("target_reference target_name does not match target_name")
    if reference.provider_id != canonical_reference.provider_id:
        raise ValueError("target_reference provider_id does not match provider_id")
    if reference.target_category != canonical_reference.target_category:
        raise ValueError("target_reference target_category does not match target_category")
    if reference.provider_target_type != canonical_reference.provider_target_type:
        raise ValueError(
            "target_reference provider_target_type does not match provider_target_type"
        )
    return canonical_reference


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
