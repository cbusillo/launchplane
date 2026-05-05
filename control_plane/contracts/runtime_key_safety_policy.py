import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RuntimeEnvironmentClass = Literal["prod", "testing", "preview", "dev", "unknown"]
RuntimeSecretClass = Literal["prod_only", "testing", "preview", "non_prod", "shared_safe"]
RuntimeKeySafetyStatus = Literal["pass", "fail"]
RuntimeKeySafetyPolicyStatus = Literal["active", "superseded"]
RuntimeKeySafetyFindingCode = Literal[
    "ambiguous_binding",
    "binding_disabled",
    "binding_missing",
    "context_not_allowed",
    "instance_not_allowed",
    "secret_class_not_allowed",
    "unclassified_binding",
    "unknown_environment_class",
]


class RuntimeKeySafetyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    environment_class: RuntimeEnvironmentClass

    @model_validator(mode="after")
    def _validate_target(self) -> "RuntimeKeySafetyTarget":
        if not self.context.strip():
            raise ValueError("runtime key safety target requires context")
        if not self.instance.strip():
            raise ValueError("runtime key safety target requires instance")
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        return self


class RuntimeSecretSafetyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    secret_class: RuntimeSecretClass
    allowed_contexts: tuple[str, ...] = ()
    allowed_instances: tuple[str, ...] = ()
    description: str = ""

    @model_validator(mode="after")
    def _validate_rule(self) -> "RuntimeSecretSafetyRule":
        binding_key = self.binding_key.strip()
        if not binding_key:
            raise ValueError("runtime secret safety rule requires binding_key")
        self.binding_key = binding_key
        self.allowed_contexts = _normalize_unique_values(
            self.allowed_contexts,
            "runtime secret safety allowed_contexts values must be non-empty",
        )
        self.allowed_instances = _normalize_unique_values(
            self.allowed_instances,
            "runtime secret safety allowed_instances values must be non-empty",
        )
        self.description = self.description.strip()
        return self


class RuntimeKeySafetyPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    status: RuntimeKeySafetyPolicyStatus = "active"
    source: str
    updated_at: str
    rules: tuple[RuntimeSecretSafetyRule, ...]

    @model_validator(mode="after")
    def _validate_record(self) -> "RuntimeKeySafetyPolicyRecord":
        if not self.record_id.strip():
            raise ValueError("runtime key safety policy record requires record_id")
        if not self.source.strip():
            raise ValueError("runtime key safety policy record requires source")
        if not self.updated_at.strip():
            raise ValueError("runtime key safety policy record requires updated_at")
        if not self.rules:
            raise ValueError("runtime key safety policy record requires at least one rule")
        binding_keys: list[str] = []
        for rule in self.rules:
            if rule.binding_key in binding_keys:
                raise ValueError("runtime key safety policy rules must be unique by binding_key")
            binding_keys.append(rule.binding_key)
        self.record_id = self.record_id.strip()
        self.source = self.source.strip()
        self.updated_at = self.updated_at.strip()
        return self

    @property
    def policy_sha256(self) -> str:
        return runtime_key_safety_policy_sha256(self)


def runtime_key_safety_policy_sha256(record: RuntimeKeySafetyPolicyRecord) -> str:
    payload = record.model_dump(mode="json", exclude={"record_id", "source", "updated_at"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RuntimeKeySafetyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RuntimeKeySafetyFindingCode
    binding_key: str = ""
    binding_id: str = ""
    secret_id: str = ""
    secret_class: str = ""
    detail: str

    @model_validator(mode="after")
    def _validate_finding(self) -> "RuntimeKeySafetyFinding":
        if not self.detail.strip():
            raise ValueError("runtime key safety finding requires detail")
        self.binding_key = self.binding_key.strip()
        self.binding_id = self.binding_id.strip()
        self.secret_id = self.secret_id.strip()
        self.secret_class = self.secret_class.strip()
        self.detail = self.detail.strip()
        return self


class RuntimeKeySafetyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RuntimeKeySafetyStatus
    target: RuntimeKeySafetyTarget
    checked_binding_keys: tuple[str, ...]
    findings: tuple[RuntimeKeySafetyFinding, ...] = ()

    @model_validator(mode="after")
    def _validate_evaluation(self) -> "RuntimeKeySafetyEvaluation":
        self.checked_binding_keys = _normalize_unique_values(
            self.checked_binding_keys,
            "runtime key safety checked binding keys must be non-empty",
        )
        if self.status == "pass" and self.findings:
            raise ValueError("passing runtime key safety evaluation cannot include findings")
        if self.status == "fail" and not self.findings:
            raise ValueError("failing runtime key safety evaluation requires findings")
        return self


def _normalize_unique_values(values: tuple[str, ...], empty_message: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError(empty_message)
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
