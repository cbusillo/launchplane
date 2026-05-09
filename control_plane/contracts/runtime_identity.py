import json

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "LAUNCHPLANE_DEPLOYMENT_RECORD_ID": identity.deployment_record_id,
        "LAUNCHPLANE_ARTIFACT_ID": identity.artifact_id,
        "LAUNCHPLANE_SOURCE_GIT_REF": identity.source_git_ref,
    }
