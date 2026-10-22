from dataclasses import dataclass
from datetime import timedelta
import base64
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.provider_target_origin import (
    ProviderTargetOrigin,
    RetainedProviderTargetOrigin,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    odoo_stable_target_replacement_operation_is_terminal,
)


class ProviderHoldAuthorityBusy(RuntimeError):
    pass


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
class ProviderOperationAttentionReference:
    record_id: str
    state: Literal["held", "unverified"] = "unverified"

    def encode(self) -> str:
        return f"provider-operation:attention:v1:{self.record_id}:{self.state}"

    @classmethod
    def parse(cls, value: str) -> "ProviderOperationAttentionReference | None":
        match = re.fullmatch(
            r"provider-operation:attention:v1:(mutation-reservation-[0-9a-f]{64}):(held|unverified)",
            value,
        )
        if match is None:
            return None
        return cls(match[1], "held" if match[2] == "held" else "unverified")


@dataclass(frozen=True)
class TestingReplacementReference:
    operation_id: str
    product: str
    context: str
    instance: str
    created_at: str

    @classmethod
    def from_record(
        cls, record: OdooStableTargetReplacementOperationRecord
    ) -> "TestingReplacementReference":
        return cls(
            record.operation_id, record.product, record.context, record.instance, record.created_at
        )

    def encode(self) -> str:
        material = [self.operation_id, self.product, self.context, self.instance, self.created_at]
        encoded = (
            base64.urlsafe_b64encode(json.dumps(material, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        return "provider-operation:testing-replacement:v1:" + encoded

    @classmethod
    def parse(cls, value: str) -> "TestingReplacementReference | None":
        prefix = "provider-operation:testing-replacement:v1:"
        if not value.startswith(prefix) or len(value) > 4096:
            return None
        try:
            encoded = value[len(prefix) :]
            material = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if (
                not isinstance(material, list)
                or len(material) != 5
                or not all(isinstance(item, str) and item for item in material)
            ):
                return None
            reference = cls(*material)
            return reference if reference.encode() == value else None
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True)
class ProviderTargetHold:
    reservation: LaunchplaneIdempotencyRecord
    origin: ProviderTargetOrigin | None
    attribution: Literal["retained", "legacy", "unavailable", "corrupt"]
    operation_nonce: UUID | None = None
    separated: bool = False
    current_target_matches: bool = False
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
    source_sha256: str = ""
    replacement_operations: tuple[OdooStableTargetReplacementOperationRecord, ...] = ()

    @property
    def unsettled_replacements(self) -> tuple[OdooStableTargetReplacementOperationRecord, ...]:
        return tuple(
            record
            for record in self.replacement_operations
            if record.status == "reconciliation_required"
            or (
                TestingReplacementReference.from_record(record).encode() in self.references
                and not odoo_stable_target_replacement_operation_is_terminal(record)
            )
        )


def provider_hold_source_sha256(material: object) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Long enough for a same-key retry or a recovery already underway to settle.
DEPLOY_FENCE_RECONCILE_GRACE = timedelta(minutes=15)


def provider_hold_is_stuck(fence: LaunchplaneIdempotencyRecord, *, checked_at: str) -> bool:
    observed_at = parse_launchplane_mutation_timestamp(checked_at, field_name="checked_at")
    if fence.state == "running":
        # The heartbeat renews a live deploy's lease, so an expired lease means it died.
        return bool(fence.lease_expires_at) and (
            parse_launchplane_mutation_timestamp(
                fence.lease_expires_at, field_name="lease_expires_at"
            )
            <= observed_at
        )
    if not fence.updated_at:
        return True
    held_since = parse_launchplane_mutation_timestamp(fence.updated_at, field_name="updated_at")
    if fence.lease_expires_at:
        # A later refused deploy re-marks an expired fence and moves updated_at;
        # the lease expiry it kept is when the fence really stopped.
        held_since = min(
            held_since,
            parse_launchplane_mutation_timestamp(
                fence.lease_expires_at, field_name="lease_expires_at"
            ),
        )
    return held_since + DEPLOY_FENCE_RECONCILE_GRACE <= observed_at


def provider_hold_classification(
    snapshot: ProviderHoldLaneSnapshot, *, checked_at: str
) -> tuple[tuple[str, bool], ...]:
    return tuple(
        sorted(
            (
                hold.reservation.record_id,
                provider_hold_is_stuck(hold.reservation, checked_at=checked_at),
            )
            for hold in snapshot.holds
        )
    )
