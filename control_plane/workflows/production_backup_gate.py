from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from control_plane import runtime_environments
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_gate import (
    ProductionBackupGateRequest,
    ProductionBackupGateWorkerRequest,
)
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateWorkerResult
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.production_backup_authority import (
    require_production_backup_authority_store,
    resolve_production_backup_authority,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_provider import (
    ProductionBackupProviderError,
    execute_production_backup_provider,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.worker_runtime_key_safety import enforce_worker_runtime_key_safety


SSH_PRIVATE_KEY = "PRODUCTION_BACKUP_SSH_PRIVATE_KEY"
SSH_KNOWN_HOSTS = "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS"


def resolve_production_backup_binding(
    record_store: object,
    request: ProductionBackupGateRequest,
) -> ProductionBackupGateWorkerRequest:
    store = require_production_backup_authority_store(record_store)
    authority = resolve_production_backup_authority(
        record_store=store,
        product=request.product,
        context=request.context,
        instance=request.instance,
        promotion_action=request.promotion_action,
        generated_at=utc_now_timestamp(),
    )
    if not authority.ready or authority.policy is None:
        raise ValueError(
            "Production backup authority is not ready: " + ",".join(authority.reason_codes)
        )
    policies = store.list_production_backup_policy_records(
        product=request.product,
        context_name=request.context,
        instance_name=request.instance,
        promotion_action=request.promotion_action,
    )
    policy = next((p for p in policies if p.record_id == authority.policy.record_id), None)
    if policy is None:
        raise ValueError("Production backup policy changed during resolution.")
    targets = {
        target.record_id: target
        for summary in authority.targets
        for target in store.list_production_backup_target_records(target_id=summary.target_id)
    }
    current_targets = {
        summary.target_id: targets.get(summary.record_id) for summary in authority.targets
    }
    source = current_targets.get(policy.fast_snapshot.source_target_id)
    destination = current_targets.get(policy.independent_backup.destination_target_id)
    if source is None or destination is None:
        raise ValueError("Production backup targets changed during resolution.")
    return ProductionBackupGateWorkerRequest(
        request=request,
        policy=policy,
        source_target=source,
        destination_target=destination,
    )


def enqueue_production_backup_gate(
    *,
    record_store: PostgresRecordStore,
    request: ProductionBackupGateRequest,
    authorization: DurableOperationAuthorization,
    operation_key: str,
) -> VeriReelProdBackupGateOperationRecord:
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    operation_id = (
        "production-backup-gate-" + hashlib.sha256(operation_key.encode()).hexdigest()[:32]
    )
    try:
        existing = record_store.read_verireel_prod_backup_gate_operation_record(operation_id)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("Production backup request conflicts with its existing request.")
        return existing
    binding = resolve_production_backup_binding(record_store, request)
    recorded_at = utc_now_timestamp()
    operation = VeriReelProdBackupGateOperationRecord(
        schema_version=3,
        operation_id=operation_id,
        product=request.product,
        context=request.context,
        instance=request.instance,
        backup_record_id=request.backup_record_id,
        request_fingerprint=fingerprint,
        request=request,
        binding=binding,
        authorization=authorization,
        created_at=recorded_at,
        updated_at=recorded_at,
    )
    persisted, _created = (
        record_store.create_verireel_prod_backup_gate_operation_record_if_no_active_record(
            operation
        )
    )
    if persisted.operation_id != operation_id or persisted.request_fingerprint != fingerprint:
        raise ValueError("Production backup record ID conflicts with an existing operation.")
    return persisted


def execute_shared_production_backup(
    *,
    record_store: object,
    binding: ProductionBackupGateWorkerRequest,
    control_plane_root: Path,
    checkpoint: Callable[[str], None],
    record_progress: Callable[[dict[str, str]], None],
) -> VeriReelProdBackupGateWorkerResult:
    def check_effect(phase: str) -> None:
        try:
            current = resolve_production_backup_binding(record_store, binding.request)
        except (ValueError, FileNotFoundError) as error:
            raise ProductionBackupProviderError("backup_authority_unavailable") from error
        if current != binding:
            raise ProductionBackupProviderError("backup_authority_changed")
        try:
            checkpoint(phase)
        except DurableOperationAuthorizationDeniedError as error:
            raise ProductionBackupProviderError(error.code) from error

    check_effect("backup_preflight")
    enforce_worker_runtime_key_safety(
        context_name=binding.request.context,
        instance_name=binding.request.instance,
        allowed_worker_keys=(SSH_PRIVATE_KEY, SSH_KNOWN_HOSTS),
        operation_name="Production backup provider",
    )
    values = runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=binding.request.context,
        instance_name=binding.request.instance,
    )
    result = execute_production_backup_provider(
        binding,
        ssh_private_key=values.get(SSH_PRIVATE_KEY, ""),
        ssh_known_hosts=values.get(SSH_KNOWN_HOSTS, ""),
        checkpoint=check_effect,
        record_progress=record_progress,
    )
    return VeriReelProdBackupGateWorkerResult(
        status=result.status,
        snapshot_name=result.evidence.get("snapshot_name", ""),
        started_at=result.started_at,
        finished_at=result.finished_at,
        evidence=result.evidence,
        detail=result.error_code,
    )
