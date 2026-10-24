from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TestingCopyPhysicalIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    application_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    container_ids: dict[str, str]
    database_user: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class TestingCopyQualificationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["sanitize", "start"]
    generation_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    hold_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    database_name: str = Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}$")
    physical: TestingCopyPhysicalIdentity
    source_key: str = ""


class TestingCopyQualificationProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resources_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    credential_boundary_commit: str = Field(pattern=r"^[a-f0-9-]{36}$")
    original_copy_verified: Literal[True]
    credentials_cleared: Literal[True]
    connections_cleared: Literal[True]
    offline_containers_absent: Literal[True]
    database_stopped: bool
    runner_stopped: bool
    web_stopped: Literal[True]
