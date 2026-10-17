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

from control_plane.production_recipient_evidence import WRITER_ACTION_MARKER, WRITER_BINDING_MARKER

WRITER_ROUTE = "/v1/products/{product}/production-recipient/testing-writer"


class RecipientWriterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_id: str = Field(min_length=1)
    action: Literal["pause", "resume"]
    pause_operation_id: str = ""
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
    pause_operation_id: str = ""
    completed: bool = False


def retained_writer_receipt(
    root: Path,
    record: ProductionRecipientRecord,
    entry: dict[str, object],
    checkpoint: Callable[[str], None],
    *,
    retry_resume: bool = False,
) -> RecipientWriterPlan:
    from typing import cast, Any
    from control_plane.production_recipient_host import run_recipient_program
    from control_plane.production_recipient_cutover import WRITER_RECEIPT_PROGRAM

    saved = cast(dict[str, Any], entry)
    host = saved["host"]
    request = RecipientWriterRequest.model_validate(saved["request"])
    projected = record.model_copy(
        update={
            "target_id": host["target_id"],
            "app_name": host["app_name"],
            "request": record.request.model_copy(
                update={"destination_server_id": host["server_id"]}
            ),
        }
    )
    fenced = bool(saved.get("fenced"))
    action = (
        "receipt_fenced"
        if fenced
        else "receipt_resume_retryable"
        if retry_resume
        else "receipt_" + request.action
    )
    evidence = run_recipient_program(
        root,
        projected,
        {
            "project": host["app_name"],
            "service": host["service"],
            "binding": saved["witness_binding"],
            "action": action,
            "inspect": True,
        },
        WRITER_RECEIPT_PROGRAM,
        checkpoint,
        custodied_read=True,
    )
    if (
        evidence.get(WRITER_ACTION_MARKER) != action
        or evidence.get(WRITER_BINDING_MARKER) != saved["witness_binding"]
    ):
        raise ValueError("The original writer completion has no terminal host witness.")
    return RecipientWriterPlan(
        product=record.product,
        context=record.context,
        plan_sha256=request.reviewed_plan_sha256,
        preparation_plan_sha256=request.preparation_plan_sha256,
        binding_id=request.binding_id,
        action=request.action,
        completed=not retry_resume and not fenced,
        pause_operation_id=saved["record_id"]
        if request.action == "pause"
        else request.pause_operation_id,
    )


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
            or (not record.activation and self.request.action != "resume")
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
            or (binding.context, binding.instance)
            not in {
                (lane.context, lane.instance)
                for lane in self.store.read_product_profile_record(self.product).lanes
                if lane.instance == "testing"
            }
            or binding.instance != "testing"
            or (
                self.request.action == "pause"
                and binding.secret_id != record.activation["writer_secret_id"]
            )
        ):
            raise ValueError("Only a recorded testing peer of this exact API budget is eligible.")
        target = self.store.read_provider_target_record(
            context_name=binding.context, instance_name="testing"
        )
        if target.provider_id != "dokploy" or target.target_category != "compose":
            raise ValueError("Testing writer control requires exact Compose authority.")
        inventory = self.store.read_environment_inventory(
            context_name=binding.context, instance_name="testing"
        )
        if inventory.runtime_identity is None:
            raise ValueError("The testing writer requires recorded exact runtime identity.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target.target_id
        )
        import re

        if payload.get("composeId") != target.target_id or not re.fullmatch(
            r"[A-Za-z0-9_-]+", str(payload.get("appName") or "")
        ):
            raise ValueError("Testing host custody requires the exact provider app identity.")
        projected = record.model_copy(
            update={
                "target_id": target.target_id,
                "app_name": str(payload.get("appName") or ""),
                "request": record.request.model_copy(
                    update={"destination_server_id": str(payload.get("serverId") or "")}
                ),
                "execution": {
                    "runtime_identity": inventory.runtime_identity.model_dump(mode="json")
                },
            }
        )
        if self.request.action == "resume":
            pauses = [
                entry
                for entry in record.writer_custody
                if entry.get("record_id") == self.request.pause_operation_id
                and entry["request"]["action"] == "pause"
                and not entry.get("fenced")
                and entry["request"]["binding_id"] == self.request.binding_id
            ]
            if (
                len(pauses) != 1
                or {
                    key: value
                    for key, value in pauses[0]["provider"].items()
                    if key != "updated_at"
                }
                != target.model_dump(mode="json", exclude={"updated_at"})
                or pauses[0]["runtime_identity"]
                != inventory.runtime_identity.model_dump(mode="json")
            ):
                raise ValueError("Resume must name the exact recorded pause operation and writer.")
            projected = projected.model_copy(
                update={
                    "execution": {
                        **projected.execution,
                        "writer_binding": pauses[0]["witness_binding"],
                    }
                }
            )
        elif self.request.pause_operation_id:
            raise ValueError("A new pause receives its own operation witness.")
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
        return (
            "production-recipient-writer:"
            + self.request.action
            + ":"
            + self.request.preparation_plan_sha256
            + ":"
            + self.request.reviewed_plan_sha256
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record = custody(self.store, self.product, self.request.context)
            if record is None or self.reconciliation_key() != reconciliation_key:
                return ProviderObservation("unknown")
            entries = [
                entry
                for entry in record.writer_custody
                if entry["operation"] == provider_operation_key
            ]
            if len(entries) != 1:
                return ProviderObservation("unknown")
            try:
                result = retained_writer_receipt(self.root, record, entries[0], lambda _phase: None)
            except Exception:
                if self.request.action != "resume":
                    raise
                retained_writer_receipt(
                    self.root, record, entries[0], lambda _phase: None, retry_resume=True
                )
                return ProviderObservation("absent", retry_safe=True)
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
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
            record, target, plan = self.current()
            if plan.plan_sha256 != self.request.reviewed_plan_sha256:
                raise ValueError("Review the exact testing writer plan.")
            require_disposable_lane(
                self.store, product=self.product, context=target.context, instance="testing"
            )
            current = custody(self.store, self.product, record.context)
            assert current is not None
            witness_binding = record.execution.get("writer_binding") or canonical_sha256(
                provider_operation_key
            )
            record = record.model_copy(
                update={"execution": {**record.execution, "writer_binding": witness_binding}}
            )
            if not any(
                entry["operation"] == provider_operation_key for entry in current.writer_custody
            ):
                replacement = current.model_copy(
                    update={
                        "writer_custody": [
                            *current.writer_custody,
                            {
                                "operation": provider_operation_key,
                                "record_id": lease.evidence_reservation().record_id,
                                "witness_binding": witness_binding,
                                "host": {
                                    "target_id": record.target_id,
                                    "server_id": record.request.destination_server_id,
                                    "app_name": record.app_name,
                                    "service": record.request.application_service,
                                },
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
                self.store, product=self.product, context=target.context, instance="testing"
            )

            def checkpoint(phase: str) -> None:
                nonlocal started
                lease.assert_current()
                self.authorize()
                if self.current()[2].plan_sha256 != plan.plan_sha256:
                    raise ValueError("Testing writer authority changed.")
                require_disposable_lane(
                    self.store, product=self.product, context=target.context, instance="testing"
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
                    result=plan.model_copy(
                        update={
                            "completed": True,
                            "pause_operation_id": lease.evidence_reservation().record_id
                            if self.request.action == "pause"
                            else self.request.pause_operation_id,
                        }
                    ).model_dump(mode="json"),
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
