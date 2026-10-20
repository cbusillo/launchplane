"""One attribution projection for shared provider holds, without recovery authority."""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.provider_target_origin import (
    ProviderTargetOrigin,
    RetainedProviderTargetOrigin,
)


class ProviderHoldAuthorityBusy(RuntimeError):
    """The short source recheck cannot wait behind a publisher holding reservation rows."""


@dataclass(frozen=True)
class ProviderOperationReference:
    record_id: str
    operation_nonce: UUID

    def encode(self) -> str:
        return f"provider-operation:v1:{self.record_id}:{self.operation_nonce}"

    @classmethod
    def parse(cls, value: str) -> "ProviderOperationReference | None":
        match = re.fullmatch(
            r"provider-operation:v1:(mutation-reservation-[0-9a-f]{64}):([0-9a-f-]{36})", value
        )
        if match is None:
            return None
        try:
            nonce = UUID(match[2])
        except ValueError:
            return None
        reference = cls(match[1], nonce)
        return reference if reference.encode() == value else None


@dataclass(frozen=True)
class ProviderTargetHold:
    reservation: LaunchplaneIdempotencyRecord
    origin: ProviderTargetOrigin | None
    attribution: Literal["retained", "legacy", "unavailable", "corrupt"]
    operation_nonce: UUID | None = None
    separated: bool = False
    current_target_matches: bool = False
    # Current records can associate attention, never establish historical custody.
    current_lanes: tuple[tuple[str, str, str], ...] = ()

    @property
    def lane(self) -> tuple[str, str, str] | None:
        if self.origin is None:
            return None
        return self.origin.product, self.origin.context, self.origin.instance

    @property
    def reference(self) -> str:
        if self.operation_nonce is None:
            return ""
        return ProviderOperationReference(self.reservation.record_id, self.operation_nonce).encode()


def bound_provider_target_origin(
    reservation: LaunchplaneIdempotencyRecord, receipt: LaunchplaneIdempotencyRecord | None
) -> RetainedProviderTargetOrigin | None:
    from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE

    if receipt is None or (
        receipt.state != "completed"
        or receipt.record_id
        != build_launchplane_mutation_reservation_id(
            scope=reservation.scope,
            route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
            idempotency_key=reservation.record_id,
        )
        or receipt.scope != reservation.scope
        or receipt.route_path != ORIGINAL_MUTATION_INTENT_ROUTE
        or receipt.idempotency_key != reservation.record_id
        or receipt.request_fingerprint != reservation.request_fingerprint
        or receipt.response_payload.get("reservation_id") != reservation.record_id
    ):
        return None
    try:
        capsule = RetainedProviderTargetOrigin.model_validate(
            receipt.response_payload.get("provider_target_origin")
        )
    except ValueError:
        return None
    return capsule if capsule.origin.target_key() == reservation.provider_target_key else None


def project_provider_target_hold(
    reservation: LaunchplaneIdempotencyRecord,
    receipt: LaunchplaneIdempotencyRecord | None,
    *,
    receipt_corrupt: bool = False,
) -> ProviderTargetHold:
    capsule = bound_provider_target_origin(reservation, receipt)
    if capsule is not None:
        return ProviderTargetHold(reservation, capsule.origin, "retained", capsule.operation_nonce)
    if receipt_corrupt or (
        receipt is not None and "provider_target_origin" in receipt.response_payload
    ):
        return ProviderTargetHold(reservation, None, "corrupt")
    from control_plane.workflows.generic_web_deploy_provider import (
        decode_generic_web_provider_reconciliation_target,
    )

    try:
        snapshot = decode_generic_web_provider_reconciliation_target(reservation.reconciliation_key)
        origin = ProviderTargetOrigin(
            product=snapshot.product,
            context=snapshot.context,
            instance=snapshot.instance,
            provider_id=snapshot.provider_id,
            target_type=snapshot.target_type,
            target_id=snapshot.target_id,
        )
        if origin.target_key() == reservation.provider_target_key:
            return ProviderTargetHold(reservation, origin, "legacy")
    except ValueError:
        pass
    return ProviderTargetHold(reservation, None, "unavailable")


@dataclass(frozen=True)
class ProviderHoldLaneSnapshot:
    product: str
    context: str
    instance: str
    holds: tuple[ProviderTargetHold, ...]
    references: tuple[str, ...] = ()
    unavailable_references: tuple[str, ...] = ()
    # Internal only. Includes exact source and separation inputs, never public material.
    source_sha256: str = ""


def provider_hold_source_sha256(material: object) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
