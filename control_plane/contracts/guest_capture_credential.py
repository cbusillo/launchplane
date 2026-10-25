from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.secret_record import SecretAuditEvent


GUEST_CAPTURE_BINDING_ACTOR = "launchplane:guest-capture-binding"
GUEST_CAPTURE_BINDING_PREFIX = "guest-capture-binding-"


class GuestCaptureBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    username: str = Field(min_length=1)
    guest_kind: Literal["lxc", "qemu"]
    guest_id: str = Field(pattern=r"^[0-9]+$")
    storage_id: str = Field(min_length=1)
    snapshot_prefix: str = Field(min_length=1)

    @property
    def event_id(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return GUEST_CAPTURE_BINDING_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


class GuestCaptureCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    boundary: GuestCaptureBoundary
    private_key_secret_id: str = Field(min_length=1)
    known_hosts_secret_id: str = Field(min_length=1)
    custody_context: str = Field(min_length=1)
    custody_instance: str = Field(min_length=1)

    @property
    def secret_ids(self) -> frozenset[str]:
        return frozenset((self.private_key_secret_id, self.known_hosts_secret_id))

    def audit_event(self, recorded_at: str) -> SecretAuditEvent:
        return SecretAuditEvent(
            event_id=self.boundary.event_id,
            secret_id=self.private_key_secret_id,
            event_type="relabelled",
            recorded_at=recorded_at,
            actor=GUEST_CAPTURE_BINDING_ACTOR,
            metadata={"guest_capture_binding": self.model_dump_json()},
        )


def guest_capture_credential_from_event(event: SecretAuditEvent) -> GuestCaptureCredential:
    if (
        event.actor != GUEST_CAPTURE_BINDING_ACTOR
        or event.event_type != "relabelled"
        or set(event.metadata) != {"guest_capture_binding"}
    ):
        raise ValueError("Guest capture binding provenance is invalid.")
    binding = GuestCaptureCredential.model_validate_json(event.metadata["guest_capture_binding"])
    if (
        event.event_id != binding.boundary.event_id
        or event.secret_id != binding.private_key_secret_id
    ):
        raise ValueError("Guest capture binding identity is invalid.")
    if len(binding.secret_ids) != 2:
        raise ValueError("Guest capture binding needs distinct credentials.")
    return binding
