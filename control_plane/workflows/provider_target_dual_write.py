from __future__ import annotations

from typing import Protocol

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


class ProviderTargetDualWriteStore(Protocol):
    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None: ...


def prepare_provider_target_from_dokploy_records(
    *,
    record_store: ProviderTargetDualWriteStore,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
) -> ProviderTargetRecord:
    provider_target_record = ProviderTargetRecord.from_dokploy_records(
        target_record=target_record,
        target_id_record=target_id_record,
    )
    current_record = _find_current_provider_target_record(
        record_store=record_store,
        context=provider_target_record.context,
        instance=provider_target_record.instance,
    )
    if current_record is not None and _authority_payload(current_record) != _authority_payload(
        provider_target_record
    ):
        raise ValueError(
            "Provider target dual-write conflict for "
            f"{provider_target_record.context}/{provider_target_record.instance}."
        )
    return provider_target_record


def write_provider_target_from_dokploy_records(
    *,
    record_store: ProviderTargetDualWriteStore,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
) -> ProviderTargetRecord:
    provider_target_record = prepare_provider_target_from_dokploy_records(
        record_store=record_store,
        target_record=target_record,
        target_id_record=target_id_record,
    )
    record_store.write_provider_target_record(provider_target_record)
    return provider_target_record


def _find_current_provider_target_record(
    *, record_store: ProviderTargetDualWriteStore, context: str, instance: str
) -> ProviderTargetRecord | None:
    return next(
        (
            record
            for record in record_store.list_physical_provider_target_records()
            if record.context == context and record.instance == instance
        ),
        None,
    )


def _authority_payload(record: ProviderTargetRecord) -> dict[str, object]:
    return {
        "provider_id": record.provider_id,
        "target_category": record.target_category,
        "target_id": record.target_id,
        "display_name": record.display_name,
        "provider_target_type": record.provider_target_type,
        "provider_evidence": dict(record.provider_evidence),
    }
