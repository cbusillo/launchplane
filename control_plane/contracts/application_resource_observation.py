from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApplicationResourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["observed", "unknown"] = "unknown"
    reason_code: Literal[
        "resource_inventory_observed",
        "recipient_binding_unavailable",
        "provider_config_unavailable",
        "provider_target_unavailable",
        "provider_target_changed",
        "registered_server_unavailable",
        "resource_inventory_unavailable",
        "resource_inventory_changed",
        "resource_inventory_unsupported",
        "resource_observation_invalid",
    ] = "resource_inventory_unavailable"
    changed_component: (
        Literal[
            "swarm_nodes",
            "service",
            "task_membership",
            "container_membership",
            "volume_membership",
            "task_metadata",
            "container_metadata",
            "volume_metadata",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    service_version: int | None = Field(default=None, ge=1)
    desired_replicas: int | None = Field(default=None, ge=0)
    task_count: int | None = Field(default=None, ge=0, le=10000)
    extant_container_count: int | None = Field(default=None, ge=0, le=10000)
    configured_volume_count: int | None = Field(default=None, ge=0, le=10000)
    referenced_volume_count: int | None = Field(default=None, ge=0, le=10000)
    nonempty_volume_count: int | None = Field(default=None, ge=0, le=10000)
    empty_volume_count: int | None = Field(default=None, ge=0, le=10000)
    unavailable_volume_count: int | None = Field(default=None, ge=0, le=10000)
    unsupported_mounts_present: bool | None = None
    image_declared_volume_count: int | None = Field(default=None, ge=0, le=10000)
    service_quiescent: bool | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> "ApplicationResourceObservation":
        counts = tuple(
            getattr(self, field)
            for field in type(self).model_fields
            if field
            not in {"status", "reason_code", "image_declared_volume_count", "changed_component"}
        )
        if self.changed_component is not None and (
            self.status != "unknown" or self.reason_code != "resource_inventory_changed"
        ):
            raise ValueError("Changed components require an unknown changed resource inventory.")
        if self.status == "unknown":
            if self.reason_code == "resource_inventory_observed" or any(
                value is not None for value in (*counts, self.image_declared_volume_count)
            ):
                raise ValueError("Unknown resource observations do not supply inventory counts.")
        elif (
            self.reason_code != "resource_inventory_observed"
            or any(value is None for value in counts)
            or self.referenced_volume_count
            != (self.empty_volume_count or 0)
            + (self.nonempty_volume_count or 0)
            + (self.unavailable_volume_count or 0)
            or (self.configured_volume_count or 0) > (self.referenced_volume_count or 0)
            or self.service_quiescent
            and (self.desired_replicas != 0 or self.extant_container_count != 0)
        ):
            raise ValueError("Observed resource inventory is incomplete.")
        return self
