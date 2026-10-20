"""Exact operator rollback and Client drill restore the retained recipient's own data."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody, authority_snapshot, authority_identity
from control_plane.production_recipient_cutover import writer_action, require_peers_parked
from control_plane.production_recipient_publication import (
    rollback_publication,
    require_owned_publication_rows,
    RecipientOwnedAuthorityChanged,
    RecipientPublication,
)
from control_plane.workflows.ship import utc_now_timestamp
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


INVERSE_ROUTE = "/v1/products/{product}/production-recipient/inverse"


def inverse_reconciliation_key(preparation: str) -> str:
    return "production-recipient-inverse:" + preparation


class RecipientInverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    publication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientInversePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    plan_sha256: str
    preparation_plan_sha256: str
    publication_sha256: str
    completed: bool = False
    rollback_status: Literal["pending", "pass", "fail"] = "pending"


def inverse_plan(
    store: PostgresRecordStore, record: ProductionRecipientRecord, request: RecipientInverseRequest
) -> RecipientInversePlan:
    if (record.context, record.plan_sha256, record.publication.get("published_sha256")) != (
        request.context,
        request.preparation_plan_sha256,
        request.publication_sha256,
    ) or record.phase not in {"published", "reverting", "inverse_published", "rolled_back"}:
        raise ValueError("Review the exact retained publication and scoped inverse.")
    if record.phase in {"published", "reverting"}:
        require_owned_publication_rows(store, record)
    digest = canonical_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "custody": record.model_dump(mode="json"),
            "authority": authority_identity(
                authority_snapshot(store, record.product, record.context)
            ),
        }
    )
    completed = record.phase == "rolled_back"
    return RecipientInversePlan(
        product=record.product,
        context=record.context,
        plan_sha256=digest,
        preparation_plan_sha256=record.plan_sha256,
        publication_sha256=request.publication_sha256,
        completed=completed,
        rollback_status="pass" if completed else "pending",
    )


@dataclass
class RecipientInverseAdapter:
    root: Path
    store: PostgresRecordStore
    record: ProductionRecipientRecord
    trace_id: str
    validate_release: Callable[[], None]
    abort_observed: bool = False
    request: RecipientInverseRequest | None = None

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.record.context,
            instance="prod",
            target_type="compose",
            target_id=self.record.target_id,
        )

    def reconciliation_key(self) -> str:
        return inverse_reconciliation_key(self.record.plan_sha256)

    def writer_binding(self, key: str) -> str:
        return canonical_sha256(
            {"publication": self.record.publication["published_sha256"], "operation": key}
        )

    def result(self, status: Literal["pass", "fail"] = "pass") -> dict[str, object]:
        result: dict[str, Any] = {
            "rollback_status": status,
            "recipient_plan_sha256": self.record.plan_sha256,
        }
        if self.request is not None:
            result = RecipientInversePlan(
                product=self.record.product,
                context=self.record.context,
                plan_sha256=self.request.reviewed_plan_sha256,
                preparation_plan_sha256=self.record.plan_sha256,
                publication_sha256=self.request.publication_sha256,
                completed=status == "pass",
                rollback_status=status,
            ).model_dump(mode="json")
        return provider_operation_response_payload(
            trace_id=self.trace_id, records={}, result=result
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        current = custody(self.store, self.record.product, self.record.context)
        if current is None or current.plan_sha256 != self.record.plan_sha256:
            return ProviderObservation("unknown")
        self.record = current
        attempt = canonical_sha256(provider_operation_key)
        if current.execution.get("inverse_aborted:" + attempt):
            return ProviderObservation("present", response_payload=self.result("fail"))
        if current.phase == "rolled_back":
            return ProviderObservation("present", response_payload=self.result())
        if (
            current.phase == "published"
            or current.phase == "reverting"
            and not current.execution.get("inverse_pause_dispatched:" + attempt)
        ):
            return ProviderObservation("absent", retry_safe=True)
        if current.phase in {"reverting", "inverse_published"}:
            binding = self.writer_binding(provider_operation_key)
            if current.phase == "reverting":
                try:
                    published = RecipientPublication.model_validate(
                        current.publication["published"]
                    )
                    if (
                        self.store.read_provider_target_record(
                            context_name=current.context, instance_name="prod"
                        )
                        != published.provider
                    ):
                        raise ValueError("The published recipient is no longer canonical.")
                    writer_action(
                        self.root,
                        current,
                        old=False,
                        action="receipt_resume",
                        checkpoint=lambda _phase: None,
                        binding=binding,
                    )
                    self.abort_observed = True
                    return ProviderObservation("absent", retry_safe=True)
                except Exception:
                    pass
            try:
                writer_action(
                    self.root,
                    current,
                    old=False,
                    action="receipt_pause",
                    checkpoint=lambda _phase: None,
                    binding=binding,
                )
                return ProviderObservation("absent", retry_safe=True)
            except Exception:
                pass
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        record = self.record
        if record.phase not in {"published", "reverting", "inverse_published", "rolled_back"}:
            raise ProviderMutationRejectedError(
                ValueError("Finish the original cutover before an inverse.")
            )
        attempt = canonical_sha256(provider_operation_key)
        binding = self.writer_binding(provider_operation_key)

        def save(**updates: object) -> None:
            nonlocal record
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Inverse custody requires an evidence-bound lease.")
            lease.assert_current()
            replacement = record.model_copy(update=updates)
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=replacement.model_dump(mode="json"),
                reservation=lease.evidence_reservation(),
            )
            record = replacement
            self.record = record

        def checkpoint(phase: str) -> None:
            lease.assert_current()
            lease.checkpoint_effect("recipient_inverse_" + phase)

        def admission_checkpoint(phase: str) -> None:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Inverse checkpoint requires an evidence-bound lease.")
            # This attempt's persisted pause dispatch ends fresh inverse admission.
            # Its receipt/restore must finish under its original lease and owned rows.
            if record.execution.get("inverse_pause_dispatched:" + attempt):
                checkpoint(phase)
                return
            self.validate_release()
            with self.store.provider_evidence_guard(
                lease.evidence_reservation, effect_admission_checkpoint=self.validate_release
            ):
                checkpoint(phase)

        def stop_checkpoint(phase: str) -> None:
            from control_plane.dokploy.post_deploy import ODOO_POST_DEPLOY_SCHEDULE_TRIGGER

            admission_checkpoint(phase)
            if phase == ODOO_POST_DEPLOY_SCHEDULE_TRIGGER and not record.execution.get(
                "inverse_pause_dispatched:" + attempt
            ):
                save(
                    execution={
                        **record.execution,
                        "inverse_pause_dispatched:" + attempt: utc_now_timestamp(),
                    }
                )

        try:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Inverse publication requires an evidence-bound worker.")
            if self.request is not None:
                reviewed = record.execution.get("inverse_review:" + attempt)
                if reviewed is None:
                    if (
                        inverse_plan(self.store, record, self.request).plan_sha256
                        != self.request.reviewed_plan_sha256
                    ):
                        raise ProviderMutationRejectedError(
                            ValueError("Review the exact current inverse before effects.")
                        )
                    save(
                        execution={
                            **record.execution,
                            "inverse_review:" + attempt: self.request.reviewed_plan_sha256,
                        }
                    )
                elif reviewed != self.request.reviewed_plan_sha256:
                    raise ProviderMutationRejectedError(
                        ValueError("Keep the original reviewed inverse and key.")
                    )
            if self.abort_observed:
                save(
                    phase="published",
                    execution={
                        **record.execution,
                        "inverse_aborted:" + attempt: utc_now_timestamp(),
                    },
                )
                return ProviderMutationOutcome(
                    202,
                    self.result("fail"),
                    provider_effect_performed=bool(
                        isinstance(lease, ProviderEvidenceLease)
                        and lease.evidence_reservation().provider_effect_started_at
                    ),
                )
            if record.phase in {"published", "reverting"}:
                if not record.execution.get("inverse_pause_dispatched:" + attempt):
                    self.validate_release()
                require_owned_publication_rows(self.store, record)
                require_peers_parked(self.root, self.store, record, admission_checkpoint)
                if record.phase == "published":
                    reservation = lease.evidence_reservation()
                    save(
                        phase="reverting",
                        execution={
                            **record.execution,
                            "reservation:" + reservation.record_id: reservation.model_dump(
                                mode="json"
                            ),
                        },
                    )
                writer_action(
                    self.root,
                    record,
                    old=False,
                    action="pause",
                    checkpoint=stop_checkpoint,
                    binding=binding,
                )
                record = rollback_publication(
                    self.store,
                    record,
                    stop_replacement=lambda: None,
                    reservation=lease.evidence_reservation(),
                    inverse_writer_binding=binding,
                )
                self.record = record
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
                updates: dict[str, Any] = {"phase": "rolled_back"}
                if self.request is not None:
                    updates["execution"] = {
                        **record.execution,
                        "native_inverse": {
                            "operation_id": lease.evidence_reservation().record_id,
                            "completed_at": utc_now_timestamp(),
                        },
                    }
                save(**updates)
            if self.request is not None and not record.execution.get("native_inverse"):
                save(
                    execution={
                        **record.execution,
                        "native_inverse": {
                            "operation_id": lease.evidence_reservation().record_id,
                            "completed_at": utc_now_timestamp(),
                        },
                    }
                )
            return ProviderMutationOutcome(
                202,
                self.result(),
                provider_effect_performed=bool(
                    isinstance(lease, ProviderEvidenceLease)
                    and lease.evidence_reservation().provider_effect_started_at
                ),
            )
        except RecipientOwnedAuthorityChanged:
            try:
                if record.execution.get("inverse_pause_dispatched:" + attempt):
                    published = RecipientPublication.model_validate(record.publication["published"])
                    if (
                        self.store.read_provider_target_record(
                            context_name=record.context, instance_name="prod"
                        )
                        != published.provider
                    ):
                        raise ValueError("Preserve changed canonical recipient custody.")
                    writer_action(
                        self.root,
                        record,
                        old=False,
                        action="resume",
                        checkpoint=checkpoint,
                        binding=binding,
                    )
                save(
                    phase="published",
                    execution={
                        **record.execution,
                        "inverse_aborted:" + attempt: utc_now_timestamp(),
                    },
                )
                return ProviderMutationOutcome(
                    202,
                    self.result("fail"),
                    provider_effect_performed=bool(
                        isinstance(lease, ProviderEvidenceLease)
                        and lease.evidence_reservation().provider_effect_started_at
                    ),
                )
            except Exception as restore_error:
                raise ProviderMutationUnknownError(
                    "Preserve the exact inverse and replacement writer witness."
                ) from restore_error
        except Exception as error:
            if not record.execution.get(
                "inverse_pause_dispatched:" + attempt
            ) and not record.publication.get("inverse_backup_policies"):
                if record.phase == "reverting":
                    save(phase="published")
                raise ProviderMutationRejectedError(
                    ValueError("The exact inverse refused before writer dispatch.")
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
