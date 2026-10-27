from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.runtime_platform_credentials import (
    find_platform_credentials,
)
from control_plane.runtime_key_safety import is_secret_shaped_runtime_key
from control_plane.provider_key_adoption import looks_like_credential
from control_plane.contracts.runtime_environment_record import normalize_retired_provider_keys


class GenericWebReleaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    values: dict[str, str] = Field(default_factory=dict)
    absent_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_settings(self) -> "GenericWebReleaseSettings":
        normalize_retired_provider_keys(tuple(self.values))
        normalize_retired_provider_keys(self.absent_keys)
        if set(self.values) & set(self.absent_keys):
            raise ValueError("Release settings cannot be both present and absent.")
        if find_platform_credentials(self.values) or any(
            is_secret_shaped_runtime_key(key) or looks_like_credential("", value)
            for key, value in self.values.items()
        ):
            raise ValueError("Release settings cannot contain platform credentials.")
        return self


class GenericWebReleaseBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    deployment_record_id: str = Field(min_length=1)
    settings: GenericWebReleaseSettings
