"""Original-key isolated cold-data recovery, preserving every serving authority."""

from dataclasses import dataclass
from pathlib import Path
import re

from control_plane.contracts.production_recipient import (
    ProductionRecipientRecord,
    RecipientRecoveryRequest,
    RecipientRecoveryPlan,
)
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import (
    ProductionRecipientAdapter,
    custody,
    qualified_render,
    runtime_values,
)
from control_plane.production_recipient_host import RECOVERY_PROGRAM, run_recipient_program
from control_plane.production_recipient_resources import resource_spec
from control_plane.production_recipient_upstreams import upstream_binding
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

RECOVERY_ROUTE = "/v1/products/{product}/production-recipient/recovery"


@dataclass
class RecipientRecoveryAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: RecipientRecoveryRequest
    actor: str
    scope: str
    trace_id: str
    original_key: str = ""

    def target_key(self) -> str:
        return "production-recipient:" + self.product + ":" + self.request.context

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def current(self) -> tuple[ProductionRecipientRecord, RecipientRecoveryPlan]:
        record = custody(self.store, self.product, self.request.context)
        if (
            record is None
            or not record.qualified_at
            or not record.target_id
            or (record.actor, record.scope, record.plan_sha256)
            != (self.actor, self.scope, self.request.preparation_plan_sha256)
        ):
            raise ValueError("Recovery requires exact original caller and qualified preparation.")
        preparation = ProductionRecipientAdapter(
            self.root,
            self.store,
            self.product,
            record.request,
            self.actor,
            self.scope,
            self.trace_id,
            record.original_key,
        )
        preparation.verify(record)
        if record.request.upstreams and record.upstream_proof.get("binding") != upstream_binding(
            record
        ):
            raise ValueError("The recipient's read-only upstream proof is unbound.")
        rendered = qualified_render(record, runtime_values(self.store, self.root, record))
        digest = canonical_sha256(
            {
                "preparation": record.plan_sha256,
                "recipient": record.target_id,
                "destination": record.request.destination_server_id,
                "image": record.image_reference,
                "database_image": rendered["services"][record.request.database_service]["image"],
                "resources": resource_spec(record),
                "authority": record.authority_snapshot,
                "upstream_proof": record.upstream_proof,
            }
        )
        proof = record.recovery_proof
        if proof and (
            proof.get("binding") != digest
            or (self.original_key and proof.get("original_key") != self.original_key)
        ):
            raise ValueError("Retain the recovery's exact binding and original key.")
        return record, RecipientRecoveryPlan(
            product=self.product,
            context=record.context,
            preparation_plan_sha256=record.plan_sha256,
            plan_sha256=digest,
            recovered=bool(proof),
            capture_sha256=str(proof.get("capture_sha256", "")),
            recovered_at=str(proof.get("recovered_at", "")),
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            _, result = self.current()
            if result.recovered and result.plan_sha256 == reconciliation_key:
                return ProviderObservation(
                    "present",
                    response_payload=provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
                    ),
                )
        except Exception:
            pass
        # Capture/restore might have happened. Never repeat it on assumed absence.
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            record, result = self.current()
            if not self.original_key or result.plan_sha256 != self.request.reviewed_plan_sha256:
                raise ValueError("Review the current recovery plan and retain its original key.")
            if record.recovery_proof:
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
                    ),
                    provider_effect_performed=False,
                )

            def checkpoint(phase: str) -> None:
                nonlocal started
                lease.assert_current()
                self.current()
                lease.checkpoint_effect(phase)
                started = True

            rendered = qualified_render(record, runtime_values(self.store, self.root, record))
            evidence = run_recipient_program(
                self.root,
                record,
                {
                    **resource_spec(record),
                    "prepare": False,
                    "recover": True,
                    "recovery_binding": result.plan_sha256,
                    "database_image": rendered["services"][record.request.database_service][
                        "image"
                    ],
                },
                RECOVERY_PROGRAM,
                checkpoint,
            )
            if (
                evidence.get("recipient_recovery_ok") != "true"
                or evidence.get("recipient_recovery_binding") != result.plan_sha256
                or re.fullmatch(r"[a-f0-9]{64}", evidence.get("recipient_recovery_capture", ""))
                is None
            ):
                raise ValueError("Capture alone, uncertain recovery or isolation cannot qualify.")
            lease.assert_current()
            self.current()
            replacement = record.model_copy(
                update={
                    "recovery_proof": {
                        "binding": result.plan_sha256,
                        "original_key": self.original_key,
                        "capture_sha256": evidence["recipient_recovery_capture"],
                        "recovered_at": utc_now_timestamp(),
                        "network": "none",
                        "verifier": "cold_mariadb_integrity",
                        "recipient": record.target_id,
                        "artifact": record.image_reference,
                    }
                }
            )
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=replacement.model_dump(mode="json"),
            )
            _, completed = self.current()
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=completed.model_dump(mode="json")
                ),
            )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    ValueError("Recovery refused before effects.")
                ) from error
            raise ProviderMutationUnknownError(
                "Retain both recipients, capture and original recovery key."
            ) from error
