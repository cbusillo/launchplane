"""Original-key isolated cold-data recovery, preserving every serving authority."""

from dataclasses import dataclass
from datetime import datetime, timezone
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
    ProviderEvidenceLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord

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
        if self.request.purpose == "published":
            from control_plane.workflows.generic_web_deploy_provider import (
                generic_web_provider_target_key,
            )

            record = custody(self.store, self.product, self.request.context)
            if record is None:
                raise ValueError("Published recipient custody is required.")
            return generic_web_provider_target_key(
                provider_id="dokploy",
                context=record.context,
                instance="prod",
                target_type="compose",
                target_id=record.target_id,
            )
        return "production-recipient:" + self.product + ":" + self.request.context

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def current(self) -> tuple[ProductionRecipientRecord, RecipientRecoveryPlan]:
        if self.request.purpose == "published":
            from control_plane.production_recipient_published_recovery import published_current

            return published_current(self)
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
            captured_database_empty=proof.get("captured_database_empty"),
        )

    def host_spec(
        self, record: ProductionRecipientRecord, result: RecipientRecoveryPlan
    ) -> dict[str, object]:
        rendered = qualified_render(
            record,
            runtime_values(
                self.store, self.root, record, published=self.request.purpose == "published"
            ),
        )
        spec = {
            **resource_spec(record),
            "prepare": False,
            "recover": True,
            "recovery_binding": result.plan_sha256,
            "database_image": rendered["services"][record.request.database_service]["image"],
        }
        if self.request.purpose == "published":
            from control_plane.production_recipient_published_recovery import published_mounts

            spec.update(
                {
                    "application_image": record.image_reference,
                    "application_service": record.request.application_service,
                    "database_service": record.request.database_service,
                    "mounts": published_mounts(record, rendered),
                }
            )
        return spec

    def host_program(self) -> str:
        if self.request.purpose == "published":
            from control_plane.production_recipient_published_host import PUBLISHED_RECOVERY_PROGRAM

            return PUBLISHED_RECOVERY_PROGRAM
        return RECOVERY_PROGRAM

    def record_host_result(
        self,
        record: ProductionRecipientRecord,
        result: RecipientRecoveryPlan,
        evidence: dict[str, str],
        reservation: LaunchplaneIdempotencyRecord | None = None,
    ) -> RecipientRecoveryPlan:
        if self.request.purpose == "published":
            from control_plane.production_recipient_published_recovery import (
                record_published_result,
            )

            return record_published_result(self, record, result, evidence, reservation)
        if (
            evidence.get("recipient_recovery_ok") != "true"
            or evidence.get("recipient_recovery_binding") != result.plan_sha256
            or re.fullmatch(r"[a-f0-9]{64}", evidence.get("recipient_recovery_capture", "")) is None
            or evidence.get("recipient_recovery_empty") not in {"true", "false"}
            or not evidence.get("recipient_recovery_epoch", "").isdigit()
        ):
            raise ValueError("Capture alone, uncertain recovery or isolation cannot qualify.")
        _, current = self.current()
        if current.recovered:
            return current
        replacement = record.model_copy(
            update={
                "recovery_proof": {
                    "binding": result.plan_sha256,
                    "original_key": self.original_key,
                    "capture_sha256": evidence["recipient_recovery_capture"],
                    "recovered_at": datetime.fromtimestamp(
                        int(evidence["recipient_recovery_epoch"]), timezone.utc
                    ).isoformat(),
                    "captured_database_empty": evidence["recipient_recovery_empty"] == "true",
                    "network": "none",
                    "verifier": "cold_mariadb_integrity",
                    "recipient": record.target_id,
                    "artifact": record.image_reference,
                }
            }
        )
        self.store.write_production_recipient(
            expected=record.model_dump(mode="json"), replacement=replacement.model_dump(mode="json")
        )
        return self.current()[1]

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record, result = self.current()
            if result.plan_sha256 != reconciliation_key or not self.original_key:
                return ProviderObservation("unknown")
            if not result.recovered:
                # Inspect only the terminal host witness under its own file lock.
                # No capture/restore is dispatched by reconciliation observation.
                def observe_checkpoint(phase: str) -> None:
                    self.current()

                evidence = run_recipient_program(
                    self.root,
                    record,
                    {**self.host_spec(record, result), "inspect": True},
                    self.host_program(),
                    observe_checkpoint,
                )
                if evidence.get("recipient_recovery_binding") != reconciliation_key:
                    return ProviderObservation("unknown")
                if evidence.get("recipient_recovery_retry_safe") == "true":
                    return ProviderObservation("absent", retry_safe=True)
                result = self.record_host_result(record, result, evidence)
            if result.recovered:
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
            if self.request.purpose == "published" and not isinstance(lease, ProviderEvidenceLease):
                raise ValueError(
                    "Published recovery requires a transactionally fenced evidence lease."
                )
            if result.recovered:
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

            evidence = run_recipient_program(
                self.root,
                record,
                self.host_spec(record, result),
                self.host_program(),
                checkpoint,
            )
            lease.assert_current()
            completed = self.record_host_result(
                record,
                result,
                evidence,
                lease.evidence_reservation() if isinstance(lease, ProviderEvidenceLease) else None,
            )
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
