"""Coordinate a recorded testing peer through its existing deploy authority."""

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody, provider_binding_digest
from control_plane.production_recipient_cutover import writer_action
from control_plane.disposable_lanes import require_disposable_lane
from control_plane.dokploy import api, source
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderOperationLease,
    ProviderEvidenceLease,
    ProviderObservation,
    provider_operation_response_payload,
)

WRITER_ROUTE = "/v1/products/{product}/production-recipient/testing-writer"


class RecipientWriterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_id: str = Field(min_length=1)
    action: Literal["pause", "resume"]
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientWriterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    plan_sha256: str
    action: Literal["pause", "resume"]
    preparation_plan_sha256: str
    binding_id: str
    completed: bool = False


@dataclass
class RecipientWriterAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: RecipientWriterRequest
    trace_id: str
    authorize: Callable[[], None] = lambda: None

    def current(
        self,
    ) -> tuple[ProductionRecipientRecord, ProviderTargetRecord, RecipientWriterPlan]:
        record = custody(self.store, self.product, self.request.context)
        if (
            record is None
            or record.plan_sha256 != self.request.preparation_plan_sha256
            or not record.activation
        ):
            raise ValueError("Testing writer control requires the exact armed production move.")
        binding = next(
            (
                row
                for row in self.store.list_secret_bindings()
                if row.binding_id == self.request.binding_id
            ),
            None,
        )
        if (
            binding is None
            or binding.context != record.context
            or binding.instance != "testing"
            or binding.secret_id != record.activation["writer_secret_id"]
        ):
            raise ValueError("Only a recorded testing peer of this exact API budget is eligible.")
        target = self.store.read_provider_target_record(
            context_name=record.context, instance_name="testing"
        )
        if target.provider_id != "dokploy" or target.target_category != "compose":
            raise ValueError("Testing writer control requires exact Compose authority.")
        inventory = self.store.read_environment_inventory(
            context_name=record.context, instance_name="testing"
        )
        if inventory.runtime_identity is None:
            raise ValueError("The testing writer requires recorded exact runtime identity.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target.target_id
        )
        projected = record.model_copy(
            update={
                "target_id": target.target_id,
                "app_name": str(payload.get("appName") or ""),
                "execution": {
                    "runtime_identity": inventory.runtime_identity.model_dump(mode="json")
                },
            }
        )
        digest = canonical_sha256(
            {
                "request": self.request.model_dump(
                    mode="json", exclude={"mode", "reviewed_plan_sha256"}
                ),
                "binding": binding.model_dump(mode="json"),
                "target": target.model_dump(mode="json"),
                "provider": provider_binding_digest(payload),
                "inventory": inventory.model_dump(mode="json"),
            }
        )
        return (
            projected,
            target,
            RecipientWriterPlan(
                product=self.product,
                context=record.context,
                plan_sha256=digest,
                action=self.request.action,
                preparation_plan_sha256=record.plan_sha256,
                binding_id=self.request.binding_id,
            ),
        )

    def target_key(self) -> str:
        _, target, _ = self.current()
        return generic_web_provider_target_key(
            provider_id=target.provider_id,
            context=target.context,
            instance=target.instance,
            target_type="compose",
            target_id=target.target_id,
        )

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record, _, plan = self.current()
            if plan.plan_sha256 != reconciliation_key:
                return ProviderObservation("unknown")
            # Host receipts require the completed stop/start response and readback,
            # not just a currently stopped/running container or an idle lock.
            writer_action(
                self.root,
                record,
                old=False,
                action="receipt_pause" if self.request.action == "pause" else "receipt_resume",
                checkpoint=lambda _phase: None,
            )
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={},
                    result=plan.model_copy(update={"completed": True}).model_dump(mode="json"),
                ),
            )
        except Exception:
            return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Writer custody requires an evidence-bound lease.")
            record, _, plan = self.current()
            if plan.plan_sha256 != self.request.reviewed_plan_sha256:
                raise ValueError("Review the exact testing writer plan.")
            current = custody(self.store, self.product, record.context)
            assert current is not None
            if not any(
                entry["operation"] == provider_operation_key for entry in current.writer_custody
            ):
                replacement = current.model_copy(
                    update={
                        "writer_custody": [
                            *current.writer_custody,
                            {
                                "operation": provider_operation_key,
                                "request": self.request.model_dump(mode="json"),
                                "provider": self.current()[1].model_dump(mode="json"),
                                "runtime_identity": record.execution["runtime_identity"],
                            },
                        ]
                    }
                )
                self.store.write_production_recipient(
                    expected=current.model_dump(mode="json"),
                    replacement=replacement.model_dump(mode="json"),
                    reservation=lease.evidence_reservation(),
                )
            require_disposable_lane(
                self.store, product=self.product, context=record.context, instance="testing"
            )

            def checkpoint(phase: str) -> None:
                nonlocal started
                lease.assert_current()
                self.authorize()
                if self.current()[2].plan_sha256 != plan.plan_sha256:
                    raise ValueError("Testing writer authority changed.")
                require_disposable_lane(
                    self.store, product=self.product, context=record.context, instance="testing"
                )
                lease.checkpoint_effect(phase)
                started = True

            writer_action(
                self.root,
                record,
                old=False,
                action=self.request.action,
                checkpoint=checkpoint,
            )
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={},
                    result=plan.model_copy(update={"completed": True}).model_dump(mode="json"),
                ),
            )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    ValueError("Testing writer safety or authority changed.")
                ) from error
            raise ProviderMutationUnknownError(
                "Retain the original testing writer key and its exact container witness."
            ) from error
