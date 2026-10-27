from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.runtime_platform_credentials import (
    find_platform_credentials,
    plain_setting_looks_like_credential,
)
from control_plane.contracts.runtime_environment_record import normalize_retired_provider_keys


class GenericWebReleaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    values: dict[str, str] = Field(default_factory=dict)
    absent_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_settings(self) -> "GenericWebReleaseSettings":
        normalize_retired_provider_keys(tuple(self.values))
        normalize_retired_provider_keys(self.absent_keys)
        if set(self.values) & set(self.absent_keys):
            raise ValueError("Release settings cannot be both present and absent.")
        if find_platform_credentials(self.values) or any(
            plain_setting_looks_like_credential(key, value) for key, value in self.values.items()
        ):
            raise ValueError("Release settings cannot contain platform credentials.")
        return self
