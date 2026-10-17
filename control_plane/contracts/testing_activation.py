"""Immutable custody of a new generic-web testing recipient and its old fence."""

from typing import Literal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord


class TestingActivationRefusalCode(StrEnum):
    QUALIFICATION_UNVERIFIED = "testing_activation_unverified"
    DEPENDENCY_UNVERIFIED = "testing_activation_dependency_unverified"
    ROUTES_REQUIRE_CUTOVER = "testing_activation_routes_require_cutover"
    AUTHORITY_CHANGED = "testing_activation_authority_changed"
    RECEIPT_UNAVAILABLE = "testing_activation_receipt_unavailable"
    RECIPIENT_UNQUALIFIED = "testing_activation_recipient_unqualified"
    CARRIED_KEYS_UNVERIFIED = "testing_activation_carried_keys_unverified"
    RECIPIENT_IDENTITY_UNVERIFIED = "testing_activation_recipient_identity_unverified"
    RECIPIENT_OPTIONS_UNVERIFIED = "testing_activation_recipient_options_unverified"
    RECIPIENT_SOURCE_UNVERIFIED = "testing_activation_recipient_source_unverified"
    RECIPIENT_SOURCE_UNMATERIALIZED = "testing_activation_recipient_source_unmaterialized"
    RECIPIENT_ENVIRONMENT_UNVERIFIED = "testing_activation_recipient_environment_unverified"
    RECIPIENT_LABELS_UNVERIFIED = "testing_activation_recipient_labels_unverified"
    RECIPIENT_RENDER_UNVERIFIED = "testing_activation_recipient_render_unverified"
    RECIPIENT_RENDER_CHANGED = "testing_activation_recipient_render_changed"


class TestingActivationRefusal(ValueError):
    """Public codes come from a closed vocabulary, never exception text."""

    def __init__(
        self,
        message: str,
        *,
        code: TestingActivationRefusalCode = TestingActivationRefusalCode.QUALIFICATION_UNVERIFIED,
    ) -> None:
        super().__init__(message)
        self.code = code

    @property
    def public_code(self) -> str:
        return self.code.value


class TestingActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    preparation_key: str = Field(min_length=1)
    # Explicit admin identification of the retained staging interlock. Legacy
    # prose never supplies this intent and the hold's type is never rewritten.
    interlock_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    # These are existing lane setting names, never volume names or port values.
    volume_keys: dict[str, str]
    port_keys: tuple[str, ...]
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class TestingActivationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    actor: str
    scope: str
    request: TestingActivationRequest
    plan_sha256: str
    namespace: str
    preparation_record_id: str
    retained_target: DokployTargetRecord
    retained_target_id: DokployTargetIdRecord
    retained_provider: ProviderTargetRecord
    retained_reservations: tuple[LaunchplaneIdempotencyRecord, ...]
    configuration_sha256: str
    configuration_version: Literal[1, 2, 3] = Field(default=1, exclude_if=lambda value: value == 1)
    generated_configuration_sha256: str = Field(default="", exclude_if=lambda value: not value)
    compose_source: str
    image_reference: str
    host_ports: tuple[int, ...] = ()
    project_id: str = ""
    environment_id: str = ""
    target_id: str = ""
    app_name: str = ""
    before_configuration_sha256: str = ""
    phase: str = "prepared"
    published: bool = False
    qualified_at: str = ""
    published_target: DokployTargetRecord | None = None


class TestingActivationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    plan_sha256: str
    source_commit: str
    resource_namespace: str
    image_reference: str
    host_ports: tuple[int, ...]
    retained_target_preserved: Literal[True] = True
    hold_preserved: Literal[True] = True
    historical_settlement: Literal["not_evaluated"] = "not_evaluated"
    production_modified: Literal[False] = False
    deployment_requested: Literal[False] = False
    applied: bool = False
    separation_verified: bool = False


class TestingActivationResult(TestingActivationPlan):
    applied: Literal[True] = True
    separation_verified: Literal[True] = True
    qualified_at: str
