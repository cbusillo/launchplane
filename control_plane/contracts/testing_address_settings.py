from pydantic import Field

from control_plane.contracts.prepared_public_site import FrozenRecord


class TestingAddressSettingsPlan(FrozenRecord):
    original_payload: str = Field(min_length=1, repr=False)
    payload: str = Field(min_length=1, repr=False)
    provider_environment_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    website_id: int = Field(gt=0)
    database_values_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
