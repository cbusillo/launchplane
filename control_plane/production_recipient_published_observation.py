"""Settle only the original published attempt, without redispatching source work."""

import json
import re
from typing import TYPE_CHECKING

from control_plane.contracts.production_recipient import RecipientRecoveryPlan
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import authority_snapshot, custody
from control_plane.production_recipient_published_host import PUBLISHED_RECOVERY_INSPECT_PROGRAM
from control_plane.production_recipient_published_recovery import (
    PublishedAuthorityChanged,
    PublishedWitnessUnqualified,
)
from control_plane.provider_operations import (
    ProviderObservation,
    build_provider_operation_key,
    provider_operation_response_payload,
)

if TYPE_CHECKING:
    from control_plane.production_recipient_recovery import RecipientRecoveryAdapter


def observe_published(
    adapter: "RecipientRecoveryAdapter", operation_key: str, binding: str
) -> ProviderObservation:
    from control_plane.production_recipient_recovery import RECOVERY_ROUTE

    try:
        record = custody(adapter.store, adapter.product, adapter.request.context)
        reservation = adapter.store.read_idempotency_record(
            scope=adapter.scope,
            route_path=RECOVERY_ROUTE.format(product=adapter.product),
            idempotency_key=adapter.original_key,
        )
        if record is None or reservation is None or not adapter.original_key:
            return ProviderObservation("unknown")
        owner = record.execution.get("published_recovery_owner:" + binding, {})
        if (
            reservation.reconciliation_key != binding
            or adapter.request.reviewed_plan_sha256 != binding
            or (record.actor, record.scope, record.plan_sha256)
            != (adapter.actor, adapter.scope, adapter.request.preparation_plan_sha256)
            or owner.get("original_key") != adapter.original_key
            or owner.get("operation") != operation_key.removeprefix("provider-operation:")
            or operation_key
            != build_provider_operation_key(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
                reconciliation_key=binding,
            )
        ):
            return ProviderObservation("unknown")
        placement = (record.namespace, record.target_id, record.request.destination_server_id)

        def checkpoint(_phase: str) -> None:
            observed = custody(adapter.store, adapter.product, adapter.request.context)
            if (
                observed is None
                or (observed.namespace, observed.target_id, observed.request.destination_server_id)
                != placement
            ):
                raise ValueError("Original recovery placement changed.")

        # Keep the adapter's existing transport patch point for callers/tests.
        from control_plane import production_recipient_recovery

        evidence = production_recipient_recovery.run_recipient_program(
            adapter.root,
            record,
            {
                "namespace": record.namespace,
                "binding": record.plan_sha256,
                "recovery_binding": binding,
                "operation": owner["operation"],
                "inspect": True,
            },
            PUBLISHED_RECOVERY_INSPECT_PROGRAM,
            checkpoint,
        )
        if (
            evidence.get("recipient_recovery_binding") != binding
            or evidence.get("recipient_recovery_operation") != owner["operation"]
        ):
            return ProviderObservation("unknown")
        failed = evidence.get("recipient_recovery_retry_safe") == "true"
        passed = evidence.get("recipient_recovery_ok") == "true"
        if not failed and not passed:
            return ProviderObservation("unknown")
        retained: dict[str, str] = {}
        if evidence.get("recipient_recovery_retained"):
            retained = json.loads(bytes.fromhex(evidence["recipient_recovery_retained"]))
            if (
                set(retained) != {"container", "id", "path", "disposition"}
                or not (
                    retained["disposition"] == "stopped_create_unconfirmed"
                    and retained["id"] == ""
                    or retained["disposition"] == "retained_or_cleanup_unconfirmed"
                    and re.fullmatch(r"[a-f0-9]{64}", retained["id"])
                )
                or not re.fullmatch(r"lp-recovery-[a-f0-9]{32}", retained["container"])
                or not retained["path"].startswith("/")
            ):
                return ProviderObservation("unknown")
        result = RecipientRecoveryPlan(
            product=adapter.product,
            context=adapter.request.context,
            preparation_plan_sha256=adapter.request.preparation_plan_sha256,
            plan_sha256=binding,
            purpose="published",
            generation=adapter.request.generation,
            retained_scratch=retained,
        )
        if passed:
            # Explicit drift alone permits historical completion. A render, I/O,
            # lease or CAS failure must not be mistaken for changed authority.
            changed = (
                canonical_sha256(authority_snapshot(adapter.store, adapter.product, record.context))
                != owner["authority_sha256"]
                or record.publication.get("published_sha256") != owner["publication_sha256"]
            )
            if not changed:
                try:
                    current_record, current = adapter.current()
                    if current.plan_sha256 == binding:
                        result = adapter.record_host_result(current_record, current, evidence)
                    # A successfully resolved different plan cannot qualify this
                    # terminal witness. Complete its original binding historically.
                except (PublishedAuthorityChanged, PublishedWitnessUnqualified):
                    pass
        return ProviderObservation(
            "present",
            response_payload=provider_operation_response_payload(
                trace_id=adapter.trace_id,
                records={},
                result=result.model_dump(mode="json"),
            ),
        )
    except Exception:
        return ProviderObservation("unknown")
