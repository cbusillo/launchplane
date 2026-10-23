import ast
from collections.abc import Callable
from dataclasses import dataclass
import re
from uuid import UUID

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentRecord,
    PublicIngressObservationRecord,
)
from control_plane.provider_target_holds import (
    ProviderOperationAttentionReference,
    ProviderOperationReference,
)


@dataclass(frozen=True)
class CapturedTestingFence:
    record_id: str
    created_at: str = ""
    operation_nonce: UUID | None = None


@dataclass(frozen=True)
class TestingFenceAbandonment:
    reservation: LaunchplaneIdempotencyRecord
    receipt: LaunchplaneIdempotencyRecord | None
    recovery_reference: str

    def serialize_material(self) -> object:
        return [
            self.reservation.model_dump(mode="json"),
            self.receipt.model_dump(mode="json") if self.receipt else None,
            self.recovery_reference,
        ]


def capture_testing_fences(
    record: PublicIngressObservationRecord,
) -> tuple[CapturedTestingFence, ...]:
    if (
        record.instance != "testing"
        or record.check_kind != "provider"
        or record.failure_code != "deploy_fence_held"
    ):
        return ()
    captures: set[CapturedTestingFence] = set()
    for target in record.targets:
        if target.target != "provider":
            continue
        match = re.fullmatch(
            r"(?:Deploy (?P<key>.+)|Operation (?P<id>mutation-reservation-[0-9a-f]{64})) "
            r"started at (?P<created>\S+) and holds this target "
            r"\((?:running|reconcile_required), last at [^,]+, since [^)]+\)\. "
            r"Every later deploy is refused until it is recovered with the generic-web deploy recovery dry-run\.",
            target.summary,
        )
        if match is None:
            match = re.search(
                r"^\d+ shared provider operations remain held for this lane; \d+ require reconciliation\. "
                r"Operation (?P<id>mutation-reservation-[0-9a-f]{64}) remains (?:running|reconcile_required), "
                r"last phase [^,]+, since (?P<created>\S+)\.(?: |$)",
                target.summary,
            )
        if match is None:
            continue
        try:
            created = format_launchplane_mutation_timestamp(
                parse_launchplane_mutation_timestamp(match["created"], field_name="created_at")
            )
            record_id = match.groupdict().get("id")
            literal = match.groupdict().get("key")
            if literal is not None:
                key = ast.literal_eval(literal)
                from control_plane.contracts.product_reconcile import RECONCILE_SOURCE
                from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
                from control_plane.product_reconcile import reconcile_reservation_scope

                if (
                    not isinstance(key, str)
                    or repr(key) != literal
                    or not key.startswith(
                        f"{RECONCILE_SOURCE}:{record.product}:{record.context}:testing:"
                    )
                ):
                    continue
                record_id = build_launchplane_mutation_reservation_id(
                    scope=reconcile_reservation_scope(record.product),
                    route_path=GENERIC_WEB_DEPLOY_ROUTE,
                    idempotency_key=key,
                )
            if record_id:
                captures.add(CapturedTestingFence(record_id, created))
        except (ValueError, SyntaxError, TypeError):
            continue
    fingerprint = record.material_fingerprint
    for value in fingerprint.route_authority.target_keys if fingerprint else ():
        reference = ProviderOperationReference.parse(value)
        if reference is not None:
            captures.add(
                CapturedTestingFence(reference.record_id, operation_nonce=reference.operation_nonce)
            )
    return tuple(
        sorted(
            captures, key=lambda item: (item.record_id, item.created_at, str(item.operation_nonce))
        )
    )


def prove_testing_fence_history(
    incident: PublicIngressIncidentRecord,
    events: tuple[PublicIngressIncidentEventRecord, ...],
    observations: tuple[PublicIngressObservationRecord, ...],
    read_abandonment: Callable[[CapturedTestingFence], TestingFenceAbandonment | None],
) -> object | None:
    if incident.instance != "testing" or incident.status != "open" or not events:
        return None
    by_id = {record.record_id: record for record in observations}
    events_by_id = {event.event_id: event for event in events}
    for event in events:
        observation = by_id.get(event.observation_id)
        if (
            event.event not in {"opened", "updated", "reminder"}
            or event.incident_id != incident.incident_id
            or observation is None
            or observation.material_fingerprint_sha256 != event.material_fingerprint_sha256
        ):
            return None
    material_events = sorted(
        (event for event in events if event.event in {"opened", "updated"}),
        key=lambda event: (
            parse_launchplane_mutation_timestamp(event.occurred_at, field_name="occurred_at"),
            event.event_id,
        ),
    )
    if not material_events:
        return None
    opening = by_id.get(incident.opened_observation_id)
    latest = by_id.get(incident.latest_observation_id)
    if (
        opening is None
        or latest is None
        or opening.observed_at != incident.opened_at
        or latest.observed_at != incident.latest_observed_at
    ):
        return None
    if (
        material_events[0].event != "opened"
        or material_events[0].observation_id != opening.record_id
        or material_events[-1].event_id != incident.latest_material_event_id
    ):
        return None
    previous = ""
    for event in material_events:
        if event.previous_material_fingerprint_sha256 != previous:
            return None
        previous = event.material_fingerprint_sha256
    if previous != incident.material_fingerprint_sha256:
        return None
    captured_by_record = {
        record.record_id: capture_testing_fences(record) for record in observations
    }
    dated_captures: dict[str, set[CapturedTestingFence]] = {}
    for observation_captures in captured_by_record.values():
        for capture in observation_captures:
            if capture.created_at:
                dated_captures.setdefault(capture.record_id, set()).add(capture)
    evidence: dict[CapturedTestingFence, TestingFenceAbandonment] = {}
    for record in sorted(
        observations,
        key=lambda item: (
            parse_launchplane_mutation_timestamp(item.observed_at, field_name="observed_at"),
            item.record_id != opening.record_id,
            item.record_id,
        ),
    ):
        if (
            (record.product, record.context, record.instance, record.check_name, record.check_kind)
            != (
                incident.product,
                incident.context,
                incident.instance,
                incident.check_name,
                incident.check_kind,
            )
            or record.incident_id != incident.incident_id
            or bool(record.incident_event_id and record.incident_event_id not in events_by_id)
            or parse_launchplane_mutation_timestamp(record.observed_at, field_name="observed_at")
            < parse_launchplane_mutation_timestamp(opening.observed_at, field_name="observed_at")
            or record.status != "fail"
            or record.failure_code != "deploy_fence_held"
            or record.purpose != "probe"
        ):
            return None
        fingerprint = record.material_fingerprint
        references = {
            value
            for value in (fingerprint.route_authority.target_keys if fingerprint else ())
            if value.startswith("provider-operation:")
        }
        captures = set(captured_by_record[record.record_id])
        if "provider-operation:unavailable" in references and not evidence:
            return None
        for value in references - {"provider-operation:unavailable"}:
            reference = ProviderOperationReference.parse(
                value
            ) or ProviderOperationAttentionReference.parse(value)
            if isinstance(reference, ProviderOperationAttentionReference):
                witnesses = dated_captures.get(reference.record_id, set())
                if len(witnesses) != 1:
                    return None
                captures.update(witnesses)
            if reference is None or not any(
                capture.record_id == reference.record_id
                and (
                    capture.operation_nonce == reference.operation_nonce
                    if isinstance(reference, ProviderOperationReference)
                    else bool(capture.created_at)
                )
                for capture in captures
            ):
                return None
        if not captures:
            if references != {"provider-operation:unavailable"} or not any(
                target.target == "provider"
                and target.summary
                == (
                    "0 shared provider operations remain held for this lane; 0 require reconciliation."
                    " Original operation or incarnation evidence is unavailable; settlement is unverified."
                )
                for target in record.targets
            ):
                return None
            continue
        for capture in sorted(
            captures, key=lambda item: (item.record_id, item.created_at, str(item.operation_nonce))
        ):
            proof = evidence.get(capture) or read_abandonment(capture)
            if proof is None:
                return None
            if parse_launchplane_mutation_timestamp(
                proof.reservation.created_at, field_name="created_at"
            ) > parse_launchplane_mutation_timestamp(record.observed_at, field_name="observed_at"):
                return None
            evidence[capture] = proof
    if not evidence:
        return None
    return [
        incident.model_dump(mode="json"),
        [event.model_dump(mode="json") for event in events],
        [record.model_dump(mode="json") for record in observations],
        [proof.serialize_material() for proof in evidence.values()],
    ]
