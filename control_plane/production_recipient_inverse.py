"""Client-started rollback/drill restores the retained recipient with its own data."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody
from control_plane.production_recipient_cutover import writer_action, require_peers_parked
from control_plane.production_recipient_publication import rollback_publication
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    ProviderEvidenceLease,
    provider_operation_response_payload,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.generic_web_promotion import _verify_health_evidence_with_identity


@dataclass
class RecipientInverseAdapter:
    root: Path
    store: PostgresRecordStore
    record: ProductionRecipientRecord
    trace_id: str
    validate_release: Callable[[], None]

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.record.context,
            instance="prod",
            target_type="compose",
            target_id=self.record.target_id,
        )

    def reconciliation_key(self) -> str:
        return "production-recipient-inverse:" + self.record.plan_sha256

    def result(self) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id,
            records={},
            result={"rollback_status": "pass", "recipient_plan_sha256": self.record.plan_sha256},
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        current = custody(self.store, self.record.product, self.record.context)
        if current is None or current.plan_sha256 != self.record.plan_sha256:
            return ProviderObservation("unknown")
        if current.phase == "rolled_back":
            return ProviderObservation("present", response_payload=self.result())
        if current.phase in {"reverting", "inverse_published"}:
            try:
                writer_action(
                    self.root,
                    current,
                    old=False,
                    action="receipt_pause",
                    checkpoint=lambda _phase: None,
                    binding=current.publication.get("writer_binding") or None,
                )
                return ProviderObservation("absent", retry_safe=True)
            except Exception:
                pass
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        record = self.record
        started = False

        def checkpoint(phase: str) -> None:
            nonlocal started
            lease.assert_current()
            lease.checkpoint_effect("recipient_inverse_" + phase)
            started = True

        try:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Inverse publication requires an evidence-bound worker.")
            if record.phase in {"published", "reverting"}:
                self.validate_release()
                if record.phase == "published":
                    replacement = record.model_copy(update={"phase": "reverting"})
                    self.store.write_production_recipient(
                        expected=record.model_dump(mode="json"),
                        replacement=replacement.model_dump(mode="json"),
                        reservation=lease.evidence_reservation(),
                    )
                    record = replacement
                require_peers_parked(self.root, self.store, record, checkpoint)
                record = rollback_publication(
                    self.store,
                    record,
                    stop_replacement=lambda: writer_action(
                        self.root,
                        record,
                        old=False,
                        action="pause",
                        checkpoint=checkpoint,
                        binding=record.publication.get("writer_binding") or None,
                    ),
                    reservation=lease.evidence_reservation(),
                )
            if record.phase not in {"inverse_published", "rolled_back"}:
                raise ValueError("No exact inverse is admitted for this recipient.")
            if record.phase != "rolled_back":
                writer_action(
                    self.root,
                    record,
                    old=True,
                    action="resume",
                    checkpoint=checkpoint,
                    binding=record.publication.get("writer_binding") or None,
                )
                before = record.publication["before"]
                identity = RuntimeIdentity.model_validate(before["inventory"]["runtime_identity"])
                health = _verify_health_evidence_with_identity(
                    HealthcheckEvidence(
                        status="pending",
                        urls=tuple(row["url"] for row in before["health_endpoints"]),
                        timeout_seconds=record.retained_target.healthcheck_timeout_seconds or 120,
                    ),
                    expected_runtime_identity=identity,
                )
                if health.status != "pass" or health.runtime_identity_status != "match":
                    raise ValueError("The retained recipient is not verified healthy.")
                replacement = record.model_copy(update={"phase": "rolled_back"})
                lease.assert_current()
                self.store.write_production_recipient(
                    expected=record.model_dump(mode="json"),
                    replacement=replacement.model_dump(mode="json"),
                    reservation=lease.evidence_reservation(),
                )
            return ProviderMutationOutcome(202, self.result())
        except Exception as error:
            if not started and not record.publication.get("inverse_backup_policies"):
                raise ProviderMutationRejectedError(
                    ValueError("The exact inverse is not ready.")
                ) from error
            raise ProviderMutationUnknownError(
                "Retain both recipients and the original inverse operation key."
            ) from error


def run_recipient_inverse(
    *,
    root: Path,
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    scope: str,
    route_path: str,
    key: str,
    trace: str,
    validate_release: Callable[[], None],
) -> str:
    adapter = RecipientInverseAdapter(root, store, record, trace, validate_release)
    outcome = run_durable_provider_operation(
        store=store,
        scope=scope,
        route_path=route_path,
        idempotency_key=key,
        request_fingerprint=canonical_sha256(
            {
                "preparation": record.plan_sha256,
                "publication": record.publication["published_sha256"],
                "release": trace,
            }
        ),
        lease_owner=trace + "-inverse",
        response_trace_id=trace,
        adapter=adapter,
    )
    return outcome.record.record_id if outcome.record is not None else ""
