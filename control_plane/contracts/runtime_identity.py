import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RuntimeIdentityStatus = Literal[
    "unchecked",
    "match",
    "mismatch",
    "missing",
    "malformed",
    "unverifiable",
]

RUNTIME_IDENTITY_ENV_KEY = "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = ""
    context: str
    instance: str
    environment_kind: str = "stable"
    deployment_record_id: str
    artifact_id: str
    source_git_ref: str
    image_reference: str = ""
    release_tuple_id: str = ""
    preview_id: str = ""
    preview_generation_id: str = ""
    deployed_at: str = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "RuntimeIdentity":
        if not self.context.strip():
            raise ValueError("runtime identity requires context")
        if not self.instance.strip():
            raise ValueError("runtime identity requires instance")
        if not self.deployment_record_id.strip():
            raise ValueError("runtime identity requires deployment_record_id")
        if not self.artifact_id.strip():
            raise ValueError("runtime identity requires artifact_id")
        if not self.source_git_ref.strip():
            raise ValueError("runtime identity requires source_git_ref")
        return self


def runtime_identity_env(identity: RuntimeIdentity) -> dict[str, str]:
    payload = identity.model_dump(mode="json", exclude_none=True)
    return {
        RUNTIME_IDENTITY_ENV_KEY: json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "LAUNCHPLANE_DEPLOYMENT_RECORD_ID": identity.deployment_record_id,
        "LAUNCHPLANE_ARTIFACT_ID": identity.artifact_id,
        "LAUNCHPLANE_SOURCE_GIT_REF": identity.source_git_ref,
    }


def parse_runtime_identity_payload(value: object) -> RuntimeIdentity | None:
    if isinstance(value, RuntimeIdentity):
        return value
    if isinstance(value, str):
        stripped_value = value.strip()
        if not stripped_value:
            return None
        try:
            decoded_value: Any = json.loads(stripped_value)
        except json.JSONDecodeError:
            return None
        return parse_runtime_identity_payload(decoded_value)
    if isinstance(value, Mapping):
        try:
            return RuntimeIdentity.model_validate(dict(value))
        except ValueError:
            return None
    return None


def runtime_identity_from_health_payload(payload: object) -> RuntimeIdentity | None:
    if not isinstance(payload, Mapping):
        return None
    for key in (
        "runtime_identity",
        "launchplane_runtime_identity",
        "launchplaneRuntimeIdentity",
        RUNTIME_IDENTITY_ENV_KEY,
    ):
        identity = parse_runtime_identity_payload(payload.get(key))
        if identity is not None:
            return identity
    return None


def health_payload_has_runtime_identity_key(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return any(
        key in payload
        for key in (
            "runtime_identity",
            "launchplane_runtime_identity",
            "launchplaneRuntimeIdentity",
            RUNTIME_IDENTITY_ENV_KEY,
        )
    )


def compare_runtime_identity(
    *, expected: RuntimeIdentity | None, observed: RuntimeIdentity | None
) -> tuple[RuntimeIdentityStatus, str]:
    if expected is None:
        return "unchecked", "Runtime identity verification was not requested."
    if observed is None:
        return "missing", "Runtime identity was not reported by the health endpoint."

    mismatches: list[str] = []
    for field_name in (
        "product",
        "context",
        "instance",
        "environment_kind",
        "deployment_record_id",
        "artifact_id",
        "source_git_ref",
        "preview_id",
        "preview_generation_id",
    ):
        expected_value = str(getattr(expected, field_name) or "")
        observed_value = str(getattr(observed, field_name) or "")
        if expected_value and observed_value != expected_value:
            mismatches.append(field_name)
    if mismatches:
        return "mismatch", "Runtime identity mismatched fields: " + ", ".join(mismatches)
    return "match", "Runtime identity matches the expected deployment record."


def health_payload_runtime_identity_status(
    *, expected: RuntimeIdentity | None, payload: object, json_parse_failed: bool = False
) -> tuple[RuntimeIdentityStatus, str, RuntimeIdentity | None]:
    if expected is None:
        status, detail = compare_runtime_identity(expected=None, observed=None)
        return status, detail, None
    if json_parse_failed:
        return (
            "unverifiable",
            "Health endpoint passed but did not return JSON runtime identity evidence.",
            None,
        )
    observed = runtime_identity_from_health_payload(payload)
    if observed is None and health_payload_has_runtime_identity_key(payload):
        return (
            "malformed",
            "Health endpoint returned runtime identity evidence that could not be parsed.",
            None,
        )
    status, detail = compare_runtime_identity(expected=expected, observed=observed)
    return status, detail, observed
