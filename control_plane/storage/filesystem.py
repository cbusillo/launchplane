import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.agent_write_intent import AgentWriteIntentRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestHeartbeat,
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    claim_every_code_work_request,
    heartbeat_every_code_work_request,
    recover_stale_every_code_work_request,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.generic_web_rollback import GenericWebRollbackPlanRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.manager_preview_approval import (
    ManagerPreviewApprovalEventRecord,
    ManagerPreviewApprovalEventWriteStatus,
)
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_resume_detail,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    ODOO_PROD_BACKUP_RESTORE_OPERATION_PHASE_SEQUENCE,
    OdooProdBackupRestoreCheckpoint,
    OdooProdBackupRestoreOperationPhase,
    OdooProdBackupRestoreOperationRecord,
    odoo_prod_backup_restore_operation_is_verification_replay_claim,
    requeue_odoo_prod_backup_restore_verification_replay,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_PHASE_SEQUENCE,
    OdooProdRetainedVolumeBackupImportCheckpoint,
    OdooProdRetainedVolumeBackupImportOperationPhase,
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.odoo_stable_lane import (
    ODOO_STABLE_LANE_BLOCKING_STATUSES,
    OdooStableLaneOperationConflictError,
    OdooStableLaneOperationKind,
    OdooStableLaneOperationOwner,
    OdooStableLaneOperationRecord,
    odoo_stable_lane_cancellation_is_allowed,
    odoo_stable_lane_operation_priority,
)
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverEventRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    build_route_binding_key,
)
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_health_monitoring_migration import (
    migrate_product_profile_health_monitoring_payload,
)
from control_plane.contracts.product_monitoring_intent_migration import (
    migrate_product_profile_monitoring_intent_payload,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.manager_preview_approval import ManagerPreviewApprovalEventConflictError
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretRotationWrite,
    SecretVersion,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import (
    sanitize_runner_host_hygiene_audit_record_for_persistence,
)
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
    build_cancelled_verireel_prod_backup_gate_record,
)
from control_plane.tenant_repository_classification import (
    plan_tenant_repository_classification_append,
)
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
    RuntimeEnvironmentDelete,
)
from control_plane.repository_human_admission import (
    plan_repository_human_role_policy_append,
    plan_tenant_technical_human_waiver_event_append,
)
from control_plane.trusted_maintenance import (
    plan_trusted_maintenance_evidence_append,
    plan_trusted_maintenance_policy_apply,
    plan_trusted_maintenance_policy_append,
)

RecordModel = TypeVar("RecordModel", bound=BaseModel)
RuntimeEnvironmentDeleteStatus = Literal["deleted", "missing", "changed"]
CurrentAuthorityDeleteStatus = Literal["deleted", "missing", "changed"]
ProviderTargetCreateStatus = Literal["created", "exists"]


class _AuthorityBundleStageEntry(BaseModel):
    action: Literal["write", "delete"]
    record_type: str
    record_id: str
    final_path: str
    step_name: str
    staged_path: str = ""
    expected_payload: dict[str, object] | None = None


class _AuthorityBundleStageManifest(BaseModel):
    schema_version: int = 1
    stage_id: str
    state: Literal["ready", "publishing"]
    entries: tuple[_AuthorityBundleStageEntry, ...]


def _utc_now_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _lease_expires_at(*, observed_at: str, lease_seconds: int) -> str:
    if lease_seconds < 1 or lease_seconds > 86_400:
        raise ValueError("Merge train controller lease_seconds must be between 1 and 86400.")
    parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        (parsed.astimezone(timezone.utc) + timedelta(seconds=lease_seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FilesystemRecordStore:
    odoo_stable_bootstrap_reservation_settle_timeout_seconds = 30.0
    odoo_stable_bootstrap_reservation_poll_seconds = 0.01
    odoo_target_replacement_reservation_settle_timeout_seconds = 30.0
    odoo_target_replacement_reservation_poll_seconds = 0.01

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._recovering_product_authority_bundle = False

    def _record_path(self, record_type: str, record_id: str) -> Path:
        return self.state_dir / record_type / f"{record_id}.json"

    def _write_model(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        with self._product_authority_bundle_lock():
            return self._write_model_locked(record_type, record_id, model)

    def _write_model_locked(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        record_path = self._record_path(record_type, record_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=record_path.parent,
            prefix=f".{record_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as record_file:
                record_file.write(
                    json.dumps(
                        model.model_dump(mode="json", exclude_none=True),
                        indent=2,
                        sort_keys=True,
                    )
                )
                record_file.flush()
                os.fsync(record_file.fileno())
            os.replace(temporary_path, record_path)
        finally:
            Path(temporary_path).unlink(missing_ok=True)
        return record_path

    @contextmanager
    def _exclusive_record_lock(self, record_type: str, record_id: str) -> Iterator[None]:
        lock_digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
        lock_path = self.state_dir / ".locks" / record_type / f"{lock_digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _create_model_if_absent(self, record_type: str, record_id: str, model: BaseModel) -> bool:
        with self._product_authority_bundle_lock():
            return self._create_model_if_absent_locked(record_type, record_id, model)

    def _create_model_if_absent_locked(
        self, record_type: str, record_id: str, model: BaseModel
    ) -> bool:
        record_path = self._record_path(record_type, record_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with record_path.open("x", encoding="utf-8") as record_file:
                record_file.write(
                    json.dumps(
                        model.model_dump(mode="json", exclude_none=True),
                        indent=2,
                        sort_keys=True,
                    )
                )
        except FileExistsError:
            return False
        return True

    def _read_model(
        self, model_type: type[RecordModel], record_type: str, record_id: str
    ) -> RecordModel:
        with self._product_authority_bundle_lock():
            return self._read_model_locked(model_type, record_type, record_id)

    def _read_model_locked(
        self, model_type: type[RecordModel], record_type: str, record_id: str
    ) -> RecordModel:
        record_path = self._record_path(record_type, record_id)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)

    def _record_dir(self, record_type: str) -> Path:
        return self.state_dir / record_type

    def _list_models(
        self, model_type: type[RecordModel], record_type: str
    ) -> tuple[RecordModel, ...]:
        with self._product_authority_bundle_lock():
            return self._list_models_locked(model_type, record_type)

    def _list_models_locked(
        self, model_type: type[RecordModel], record_type: str
    ) -> tuple[RecordModel, ...]:
        record_dir = self._record_dir(record_type)
        if not record_dir.exists():
            return ()

        records: list[RecordModel] = []
        for record_path in sorted(record_dir.glob("*.json")):
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            records.append(model_type.model_validate(payload))
        return tuple(records)

    def _product_authority_bundle_stage_root(self) -> Path:
        return self.state_dir / ".product_authority_bundle_stages"

    def _after_product_authority_bundle_step(self, step_name: str) -> None:
        _ = step_name

    @contextmanager
    def _product_authority_bundle_lock(self) -> Iterator[None]:
        with self._exclusive_record_lock("product_authority_bundles", "global"):
            self._recover_product_authority_bundle_stages_locked()
            yield

    def _recover_product_authority_bundle_stages_locked(self) -> None:
        if self._recovering_product_authority_bundle:
            return
        stage_root = self._product_authority_bundle_stage_root()
        if not stage_root.exists():
            return
        self._recovering_product_authority_bundle = True
        try:
            for stage_dir in sorted(path for path in stage_root.iterdir() if path.is_dir()):
                manifest_path = stage_dir / "manifest.json"
                if not manifest_path.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    continue
                manifest = _AuthorityBundleStageManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
                if manifest.state == "ready":
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    continue
                self._publish_product_authority_bundle_stage(
                    manifest=manifest,
                    stage_dir=stage_dir,
                    recovering=True,
                )
            with suppress(OSError):
                stage_root.rmdir()
        finally:
            self._recovering_product_authority_bundle = False

    def write_product_authority_bundle(self, bundle: ProductAuthorityBundle) -> None:
        if not bundle.requires_write():
            return
        with self._product_authority_bundle_lock():
            stage_id = f"{_utc_now_timestamp().replace(':', '').replace('-', '')}-{time.time_ns()}"
            stage_dir = self._product_authority_bundle_stage_root() / stage_id
            records_dir = stage_dir / "records"
            records_dir.mkdir(parents=True, exist_ok=False)
            entries: list[_AuthorityBundleStageEntry] = []
            try:
                self._stage_product_authority_bundle_entries(
                    bundle=bundle,
                    stage_dir=stage_dir,
                    entries=entries,
                )
                manifest = _AuthorityBundleStageManifest(
                    stage_id=stage_id,
                    state="ready",
                    entries=tuple(entries),
                )
                self._write_product_authority_bundle_stage_manifest(
                    stage_dir=stage_dir,
                    manifest=manifest,
                )
                self._after_product_authority_bundle_step("stage_product_authority_bundle")
                publishing_manifest = manifest.model_copy(update={"state": "publishing"})
                self._write_product_authority_bundle_stage_manifest(
                    stage_dir=stage_dir,
                    manifest=publishing_manifest,
                )
                self._after_product_authority_bundle_step("publish_product_authority_bundle")
                self._publish_product_authority_bundle_stage(
                    manifest=publishing_manifest,
                    stage_dir=stage_dir,
                    recovering=False,
                )
            except Exception:
                if not (stage_dir / "manifest.json").exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
                raise

    def _stage_product_authority_bundle_entries(
        self,
        *,
        bundle: ProductAuthorityBundle,
        stage_dir: Path,
        entries: list[_AuthorityBundleStageEntry],
    ) -> None:
        for delete_item in bundle.delete_runtime_environments:
            self._stage_product_authority_bundle_delete(
                entries=entries,
                record_type="launchplane_runtime_environments",
                record_id=_runtime_environment_record_id(delete_item.expected_record),
                expected_model=delete_item.expected_record,
                step_name="delete_runtime_environment",
            )
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_runtime_environment_delete_events",
                record_id=delete_item.event.event_id,
                model=delete_item.event,
                step_name="write_runtime_environment_delete_event",
            )
        for provider_target_delete in bundle.delete_provider_targets:
            self._stage_product_authority_bundle_delete(
                entries=entries,
                record_type="launchplane_provider_targets",
                record_id=_context_instance_record_id(
                    provider_target_delete.context,
                    provider_target_delete.instance,
                ),
                expected_model=provider_target_delete,
                step_name="delete_provider_target",
            )
        for target_id_delete in bundle.delete_dokploy_target_ids:
            self._stage_product_authority_bundle_delete(
                entries=entries,
                record_type="dokploy_target_ids",
                record_id=_context_instance_record_id(
                    target_id_delete.context,
                    target_id_delete.instance,
                ),
                expected_model=target_id_delete,
                step_name="delete_dokploy_target_id",
            )
        for target_delete in bundle.delete_dokploy_targets:
            self._stage_product_authority_bundle_delete(
                entries=entries,
                record_type="dokploy_targets",
                record_id=_context_instance_record_id(
                    target_delete.context, target_delete.instance
                ),
                expected_model=target_delete,
                step_name="delete_dokploy_target",
            )

        for profile_record in bundle.product_profiles:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_product_profiles",
                record_id=profile_record.product,
                model=profile_record,
                step_name="write_product_profile",
            )
        for target_record in bundle.dokploy_targets:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="dokploy_targets",
                record_id=_context_instance_record_id(
                    target_record.context, target_record.instance
                ),
                model=target_record,
                step_name="write_dokploy_target",
            )
        for target_id_record in bundle.dokploy_target_ids:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="dokploy_target_ids",
                record_id=_context_instance_record_id(
                    target_id_record.context,
                    target_id_record.instance,
                ),
                model=target_id_record,
                step_name="write_dokploy_target_id",
            )
        for provider_target_write in bundle.provider_target_writes:
            self._validate_provider_target_write(provider_target_write)
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_provider_targets",
                record_id=_context_instance_record_id(
                    provider_target_write.record.context,
                    provider_target_write.record.instance,
                ),
                model=provider_target_write.record,
                step_name="write_provider_target",
            )
        for runtime_record in bundle.runtime_environments:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_runtime_environments",
                record_id=_runtime_environment_record_id(runtime_record),
                model=runtime_record,
                step_name="write_runtime_environment",
            )
        for version in bundle.secret_versions:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_secret_versions",
                record_id=version.version_id,
                model=version,
                step_name="write_secret_version",
            )
        for secret_record in bundle.secret_records:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_secrets",
                record_id=secret_record.secret_id,
                model=secret_record,
                step_name="write_secret_record",
            )
        for binding in bundle.secret_bindings:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_secret_bindings",
                record_id=binding.binding_id,
                model=binding,
                step_name="write_secret_binding",
            )
        for event in bundle.secret_audit_events:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="launchplane_secret_audit_events",
                record_id=event.event_id,
                model=event,
                step_name="write_secret_audit_event",
            )
        for inventory_record in bundle.environment_inventory:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="inventory",
                record_id=f"{inventory_record.context}-{inventory_record.instance}",
                model=inventory_record,
                step_name="write_environment_inventory",
            )
        for release_record in bundle.release_tuples:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="release_tuples",
                record_id=f"{release_record.context}-{release_record.channel}",
                model=release_record,
                step_name="write_release_tuple",
            )
        if bundle.idempotency_record is not None:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type="idempotency",
                record_id=bundle.idempotency_record.record_id,
                model=bundle.idempotency_record,
                step_name="write_idempotency",
            )

    def _stage_product_authority_bundle_write(
        self,
        *,
        stage_dir: Path,
        entries: list[_AuthorityBundleStageEntry],
        record_type: str,
        record_id: str,
        model: BaseModel,
        step_name: str,
    ) -> None:
        payload = model.model_dump(mode="json", exclude_none=True)
        staged_path = stage_dir / "records" / f"{len(entries):04d}.json"
        self._write_json_file(staged_path, payload)
        entry = _AuthorityBundleStageEntry(
            action="write",
            record_type=record_type,
            record_id=record_id,
            final_path=self._record_path(record_type, record_id)
            .relative_to(self.state_dir)
            .as_posix(),
            staged_path=staged_path.relative_to(stage_dir).as_posix(),
            expected_payload=payload,
            step_name=step_name,
        )
        entries.append(entry)
        self._after_product_authority_bundle_step(f"stage_{step_name}")

    def _validate_provider_target_write(self, write: ProviderTargetWrite) -> None:
        record_path = self._record_path(
            "launchplane_provider_targets",
            _context_instance_record_id(write.record.context, write.record.instance),
        )
        current_payload = self._read_json_file(record_path)
        if write.expected_absent:
            if current_payload is not None:
                raise ValueError(
                    "Provider target record was created after authority bundle planning."
                )
            return
        expected_record = write.expected_record
        if expected_record is None:
            raise ValueError("Provider target write expectation is missing.")
        expected_payload = expected_record.model_dump(mode="json", exclude_none=True)
        if current_payload != expected_payload:
            raise ValueError("Provider target record changed after authority bundle planning.")

    def _stage_product_authority_bundle_delete(
        self,
        *,
        entries: list[_AuthorityBundleStageEntry],
        record_type: str,
        record_id: str,
        expected_model: BaseModel,
        step_name: str,
    ) -> None:
        expected_payload = expected_model.model_dump(mode="json", exclude_none=True)
        final_path = self._record_path(record_type, record_id)
        current_payload = self._read_json_file(final_path)
        if current_payload is None:
            raise FileNotFoundError(f"{record_type} record {record_id} was already deleted.")
        if current_payload != expected_payload:
            raise ValueError(f"{record_type} record {record_id} changed during bundle write.")
        entries.append(
            _AuthorityBundleStageEntry(
                action="delete",
                record_type=record_type,
                record_id=record_id,
                final_path=final_path.relative_to(self.state_dir).as_posix(),
                expected_payload=expected_payload,
                step_name=step_name,
            )
        )
        self._after_product_authority_bundle_step(f"stage_{step_name}")

    def _write_product_authority_bundle_stage_manifest(
        self, *, stage_dir: Path, manifest: _AuthorityBundleStageManifest
    ) -> None:
        self._write_json_file(
            stage_dir / "manifest.json",
            manifest.model_dump(mode="json", exclude_none=True),
        )

    def _publish_product_authority_bundle_stage(
        self,
        *,
        manifest: _AuthorityBundleStageManifest,
        stage_dir: Path,
        recovering: bool,
    ) -> None:
        for entry in manifest.entries:
            final_path = self.state_dir / entry.final_path
            if entry.action == "write":
                self._publish_product_authority_bundle_write_entry(
                    entry=entry,
                    stage_dir=stage_dir,
                    final_path=final_path,
                )
            else:
                self._publish_product_authority_bundle_delete_entry(
                    entry=entry,
                    final_path=final_path,
                    recovering=recovering,
                )
            self._after_product_authority_bundle_step(entry.step_name)
        shutil.rmtree(stage_dir, ignore_errors=True)

    def _publish_product_authority_bundle_write_entry(
        self,
        *,
        entry: _AuthorityBundleStageEntry,
        stage_dir: Path,
        final_path: Path,
    ) -> None:
        staged_path = stage_dir / entry.staged_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if staged_path.exists():
            os.replace(staged_path, final_path)
            return
        current_payload = self._read_json_file(final_path)
        if current_payload != entry.expected_payload:
            raise RuntimeError(
                f"Cannot recover authority bundle write for {entry.record_type}/{entry.record_id}."
            )

    def _publish_product_authority_bundle_delete_entry(
        self,
        *,
        entry: _AuthorityBundleStageEntry,
        final_path: Path,
        recovering: bool,
    ) -> None:
        current_payload = self._read_json_file(final_path)
        if current_payload is None:
            return
        if current_payload != entry.expected_payload:
            action = "recover" if recovering else "publish"
            raise RuntimeError(
                f"Cannot {action} authority bundle delete for "
                f"{entry.record_type}/{entry.record_id}; live record changed."
            )
        with suppress(FileNotFoundError):
            final_path.unlink()

    @staticmethod
    def _write_json_file(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with temporary_path.open("w", encoding="utf-8") as record_file:
            record_file.write(json.dumps(payload, indent=2, sort_keys=True))
            record_file.flush()
            os.fsync(record_file.fileno())
        os.replace(temporary_path, path)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Record file {path} does not contain a JSON object.")
        return payload

    @staticmethod
    def _record_sort_timestamp(finished_at: str, started_at: str) -> tuple[str, str]:
        return finished_at or started_at, started_at

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> Path:
        return self._write_model("artifacts", manifest.artifact_id, manifest)

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        return ArtifactIdentityManifest.model_validate(
            self._read_model(ArtifactIdentityManifest, "artifacts", artifact_id).model_dump(
                mode="json"
            )
        )

    def list_artifact_manifests(self) -> tuple[ArtifactIdentityManifest, ...]:
        records = list(self._list_models(ArtifactIdentityManifest, "artifacts"))
        records.sort(key=lambda record: record.artifact_id, reverse=True)
        return tuple(records)

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> Path:
        return self._write_model("release_tuples", f"{record.context}-{record.channel}", record)

    def write_runtime_key_safety_policy_record(self, record: RuntimeKeySafetyPolicyRecord) -> Path:
        return self._write_model(
            "launchplane_runtime_key_safety_policies", record.record_id, record
        )

    def write_runner_host_hygiene_audit_record(
        self, record: RunnerHostHygieneApplyAuditRecord
    ) -> Path:
        persisted_record = sanitize_runner_host_hygiene_audit_record_for_persistence(record)
        return self._write_model(
            "launchplane_runner_host_hygiene_audits",
            _runner_host_hygiene_audit_record_id(persisted_record.audit_record_key),
            persisted_record,
        )

    def write_runner_lane_registration_audit_record(
        self, record: RunnerLaneRegistrationAuditRecord
    ) -> Path:
        return self._write_model(
            "launchplane_runner_lane_registration_audits",
            _runner_lane_registration_audit_record_id(record.audit_record_key),
            record,
        )

    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> Path:
        return self._write_model("launchplane_agent_write_intents", record.record_id, record)

    def write_tenant_repository_classification_record(
        self, record: TenantRepositoryClassificationRecord
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_tenant_repository_classifications"
        with self._product_authority_bundle_lock():
            records = tuple(
                existing
                for existing in self._list_models_locked(
                    TenantRepositoryClassificationRecord,
                    record_type,
                )
                if existing.repository_id == record.repository_id
            )
            plan = plan_tenant_repository_classification_append(
                records=records,
                record=record,
            )
            if plan.status == "replayed":
                return "replayed"
            self._write_model_locked(record_type, record.record_id, record)
            return "written"

    def read_tenant_repository_classification_record(
        self, record_id: str
    ) -> TenantRepositoryClassificationRecord:
        return self._read_model(
            TenantRepositoryClassificationRecord,
            "launchplane_tenant_repository_classifications",
            record_id,
        )

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                TenantRepositoryClassificationRecord,
                "launchplane_tenant_repository_classifications",
            )
            if not repository_id or record.repository_id == repository_id
        ]
        records.sort(
            key=lambda record: (
                record.classification_revision,
                record.repository_id,
                record.record_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def latest_tenant_repository_classification_lookup(
        self, *, repository_id: str
    ) -> TenantRepositoryClassificationLookup:
        records = self.list_tenant_repository_classification_records(
            repository_id=repository_id,
        )
        if not records:
            return TenantRepositoryClassificationLookup(status="missing")
        latest_revision = records[0].classification_revision
        return TenantRepositoryClassificationLookup(
            records=tuple(
                record for record in records if record.classification_revision == latest_revision
            )
        )

    def write_repository_human_role_policy_record(
        self,
        record: RepositoryHumanRolePolicyRecord,
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_repository_human_role_policies"
        with self._product_authority_bundle_lock():
            records = self._list_models_locked(RepositoryHumanRolePolicyRecord, record_type)
            plan = plan_repository_human_role_policy_append(records=records, record=record)
            if plan.status == "replayed":
                return "replayed"
            if plan.superseded_current_record is None:
                self._write_model_locked(record_type, record.record_id, record)
                return "written"
            self._write_repository_human_role_policy_replacement_locked(
                record_type=record_type,
                superseded_current_record=plan.superseded_current_record,
                active_record=record,
            )
            return "written"

    def _write_repository_human_role_policy_replacement_locked(
        self,
        *,
        record_type: str,
        superseded_current_record: RepositoryHumanRolePolicyRecord,
        active_record: RepositoryHumanRolePolicyRecord,
    ) -> None:
        stage_id = f"{_utc_now_timestamp().replace(':', '').replace('-', '')}-{time.time_ns()}"
        stage_dir = self._product_authority_bundle_stage_root() / stage_id
        records_dir = stage_dir / "records"
        records_dir.mkdir(parents=True, exist_ok=False)
        entries: list[_AuthorityBundleStageEntry] = []
        try:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type=record_type,
                record_id=superseded_current_record.record_id,
                model=superseded_current_record,
                step_name="supersede_repository_human_role_policy",
            )
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type=record_type,
                record_id=active_record.record_id,
                model=active_record,
                step_name="write_repository_human_role_policy",
            )
            manifest = _AuthorityBundleStageManifest(
                stage_id=stage_id,
                state="ready",
                entries=tuple(entries),
            )
            self._write_product_authority_bundle_stage_manifest(
                stage_dir=stage_dir,
                manifest=manifest,
            )
            self._after_product_authority_bundle_step(
                "stage_repository_human_role_policy_replacement"
            )
            publishing_manifest = manifest.model_copy(update={"state": "publishing"})
            self._write_product_authority_bundle_stage_manifest(
                stage_dir=stage_dir,
                manifest=publishing_manifest,
            )
            self._after_product_authority_bundle_step(
                "publish_repository_human_role_policy_replacement"
            )
            self._publish_product_authority_bundle_stage(
                manifest=publishing_manifest,
                stage_dir=stage_dir,
                recovering=False,
            )
        except Exception:
            if not (stage_dir / "manifest.json").exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def read_repository_human_role_policy_record(
        self,
        record_id: str,
    ) -> RepositoryHumanRolePolicyRecord:
        return self._read_model(
            RepositoryHumanRolePolicyRecord,
            "launchplane_repository_human_role_policies",
            record_id,
        )

    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = [
            record
            for record in self._list_models(
                RepositoryHumanRolePolicyRecord,
                "launchplane_repository_human_role_policies",
            )
            if (not repository_id or record.repository_id == repository_id)
            and (not repository_owner_id or record.repository_owner_id == repository_owner_id)
            and (not normalized_repository or record.repository == normalized_repository)
            and (not product or record.product == product)
            and (not context or record.context == context)
            and (not status or record.status == status)
        ]
        records.sort(
            key=lambda record: (
                record.role_policy_revision,
                record.repository_id,
                record.product,
                record.context,
                record.record_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_tenant_technical_human_waiver_event_record(
        self,
        record: TenantTechnicalHumanWaiverEventRecord,
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_tenant_technical_human_waiver_events"
        with self._product_authority_bundle_lock():
            records = self._list_models_locked(
                TenantTechnicalHumanWaiverEventRecord,
                record_type,
            )
            plan = plan_tenant_technical_human_waiver_event_append(
                records=records,
                record=record,
            )
            if plan.status == "replayed":
                return "replayed"
            self._write_model_locked(record_type, record.event_id, record)
            return "written"

    def read_tenant_technical_human_waiver_event_record(
        self,
        event_id: str,
    ) -> TenantTechnicalHumanWaiverEventRecord:
        return self._read_model(
            TenantTechnicalHumanWaiverEventRecord,
            "launchplane_tenant_technical_human_waiver_events",
            event_id,
        )

    def list_tenant_technical_human_waiver_event_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        waiver_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        role_policy_record_id: str = "",
        role_policy_digest: str = "",
        authz_policy_record_id: str = "",
        authz_policy_digest: str = "",
        action: str = "",
        author_github_id: int | None = None,
        limit: int | None = None,
    ) -> tuple[TenantTechnicalHumanWaiverEventRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = [
            record
            for record in self._list_models(
                TenantTechnicalHumanWaiverEventRecord,
                "launchplane_tenant_technical_human_waiver_events",
            )
            if (not repository_id or record.binding.repository_id == repository_id)
            and (
                not repository_owner_id or record.binding.repository_owner_id == repository_owner_id
            )
            and (not normalized_repository or record.binding.repository == normalized_repository)
            and (not product or record.binding.product == product)
            and (not context or record.binding.context == context)
            and (not waiver_id or record.waiver_id == waiver_id)
            and (not binding_sha256 or record.binding.binding_sha256 == binding_sha256)
            and (
                pull_request_number is None
                or record.binding.pull_request_number == pull_request_number
            )
            and (not head_sha or record.binding.head_sha == head_sha)
            and (
                not classification_digest
                or record.binding.classification_digest == classification_digest
            )
            and (
                not role_policy_record_id
                or record.binding.role_policy_record_id == role_policy_record_id
            )
            and (not role_policy_digest or record.binding.role_policy_digest == role_policy_digest)
            and (
                not authz_policy_record_id
                or record.binding.authz_policy_record_id == authz_policy_record_id
            )
            and (
                not authz_policy_digest or record.binding.authz_policy_digest == authz_policy_digest
            )
            and (not action or record.action == action)
            and (
                author_github_id is None
                or record.authorization.author_github_id == author_github_id
            )
        ]
        records.sort(
            key=lambda record: (
                record.occurred_at,
                record.event_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_agent_write_intent_record(self, record_id: str) -> AgentWriteIntentRecord:
        return AgentWriteIntentRecord.model_validate(
            self._read_model(
                AgentWriteIntentRecord,
                "launchplane_agent_write_intents",
                record_id,
            ).model_dump(mode="json")
        )

    def list_agent_write_intent_records(
        self,
        *,
        status: str = "",
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AgentWriteIntentRecord, ...]:
        records = [
            record
            for record in self._list_models(
                AgentWriteIntentRecord,
                "launchplane_agent_write_intents",
            )
            if (not status or record.evaluation.status == status)
            and (not product or record.evaluation.product == product)
            and (not context_name or record.evaluation.context == context_name)
        ]
        records.sort(key=lambda record: (record.recorded_at, record.record_id), reverse=True)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_merge_train_run_record(self, record: MergeTrainRunRecord) -> Path:
        return self._write_model("launchplane_merge_train_runs", record.run_id, record)

    def write_merge_train_policy_record(self, record: MergeTrainPolicyRecord) -> Path:
        return self._write_model("launchplane_merge_train_policies", record.record_id, record)

    def write_merge_train_pr_feedback_record(self, record: MergeTrainPrFeedbackRecord) -> Path:
        return self._write_model("launchplane_merge_train_pr_feedback", record.feedback_id, record)

    def list_merge_train_pr_feedback_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeTrainPrFeedbackRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainPrFeedbackRecord,
                "launchplane_merge_train_pr_feedback",
            )
            if (not repository or record.repository == repository)
            and (not base_branch or record.base_branch == base_branch)
            and (pr_number is None or record.pull_request_number == pr_number)
        ]
        records.sort(key=lambda record: (record.recorded_at, record.feedback_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_merge_train_batch_candidate_record(
        self, record: MergeTrainBatchCandidateRecord
    ) -> Path:
        return self._write_model(
            "launchplane_merge_train_batch_candidates", record.record_id, record
        )

    def write_merge_train_controller_state_record(
        self, record: MergeTrainControllerStateRecord
    ) -> Path:
        record_type = "launchplane_merge_train_controller_states"
        with self._exclusive_record_lock(record_type, record.controller_key):
            return self._write_model_locked(
                record_type,
                _merge_train_controller_storage_id(record.controller_key),
                record,
            )

    def read_merge_train_controller_state_record(
        self, controller_key: str
    ) -> MergeTrainControllerStateRecord:
        return MergeTrainControllerStateRecord.model_validate(
            self._read_model(
                MergeTrainControllerStateRecord,
                "launchplane_merge_train_controller_states",
                _merge_train_controller_storage_id(controller_key),
            ).model_dump(mode="json")
        )

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainControllerStateRecord,
                "launchplane_merge_train_controller_states",
            )
            if (not repository or record.repository == repository)
            and (not base_branch or record.base_branch == base_branch)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.controller_key), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def acquire_merge_train_controller_state_record(
        self,
        *,
        repository: str,
        base_branch: str,
        policy_key: str,
        policy_sha256: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord:
        from control_plane.contracts.merge_train_controller_state import (
            build_merge_train_controller_key,
            build_merge_train_controller_state_record,
        )

        record_type = "launchplane_merge_train_controller_states"
        controller_key = build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        )
        storage_id = _merge_train_controller_storage_id(controller_key)
        with self._exclusive_record_lock(record_type, controller_key):
            observed_at = _utc_now_timestamp()
            record_path = self._record_path(record_type, storage_id)
            current_record = (
                self._read_model_locked(
                    MergeTrainControllerStateRecord,
                    record_type,
                    storage_id,
                )
                if record_path.exists()
                else build_merge_train_controller_state_record(
                    repository=repository,
                    base_branch=base_branch,
                    policy_key=policy_key,
                    policy_sha256=policy_sha256,
                    updated_at=observed_at,
                )
            )
            if (
                current_record.status == "running"
                and current_record.lease_expires_at
                and current_record.lease_expires_at > observed_at
            ):
                raise MergeTrainControllerLeaseHeldError(
                    "merge train controller lease is held by another owner"
                )
            adopting = current_record.status == "reconcile_required" or bool(
                current_record.active_action and current_record.active_phase
            )
            leased_record = current_record.model_copy(
                update={
                    "policy_key": policy_key,
                    "policy_sha256": policy_sha256,
                    "status": "running",
                    "updated_at": observed_at,
                    "lease_owner": lease_owner,
                    "lease_acquired_at": observed_at,
                    "lease_expires_at": _lease_expires_at(
                        observed_at=observed_at,
                        lease_seconds=lease_seconds,
                    ),
                    "heartbeat_at": observed_at,
                    "active_action": current_record.active_action or "controller_run_once",
                    "active_phase": current_record.active_phase or "select_next_action",
                    "reconciliation_status": "adopted" if adopting else "clean",
                    "reconciliation_detail": (
                        build_merge_train_controller_resume_detail(current_record)
                        if adopting
                        else ""
                    ),
                }
            )
            leased_record = MergeTrainControllerStateRecord.model_validate(
                leased_record.model_dump(mode="json")
            )
            self._write_model_locked(record_type, storage_id, leased_record)
            return leased_record

    def compare_and_set_merge_train_controller_state_record(
        self,
        *,
        record: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        expected_lease_acquired_at: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord:
        record_type = "launchplane_merge_train_controller_states"
        storage_id = _merge_train_controller_storage_id(record.controller_key)
        with self._exclusive_record_lock(record_type, record.controller_key):
            observed_at = _utc_now_timestamp()
            try:
                current_record = self._read_model_locked(
                    MergeTrainControllerStateRecord,
                    record_type,
                    storage_id,
                )
            except FileNotFoundError as error:
                raise MergeTrainControllerLeaseLostError(
                    "merge train controller state is missing"
                ) from error
            if current_record.lease_owner != expected_lease_owner:
                raise MergeTrainControllerLeaseLostError(
                    "merge train controller lease owner changed"
                )
            if current_record.lease_acquired_at != expected_lease_acquired_at:
                raise MergeTrainControllerLeaseLostError(
                    "merge train controller lease token changed"
                )
            if (
                not current_record.lease_expires_at
                or current_record.lease_expires_at <= observed_at
            ):
                raise MergeTrainControllerLeaseLostError("merge train controller lease expired")
            persisted_record = record.model_copy(
                update={
                    "updated_at": observed_at,
                    **(
                        {
                            "heartbeat_at": observed_at,
                            "lease_expires_at": _lease_expires_at(
                                observed_at=observed_at,
                                lease_seconds=lease_seconds,
                            ),
                        }
                        if record.status == "running"
                        else {"last_transition_at": observed_at}
                    ),
                }
            )
            persisted_record = MergeTrainControllerStateRecord.model_validate(
                persisted_record.model_dump(mode="json")
            )
            self._write_model_locked(record_type, storage_id, persisted_record)
            return persisted_record

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainBatchCandidateRecord,
                "launchplane_merge_train_batch_candidates",
            )
            if (not repository or record.candidate.repository == repository)
            and (not base_branch or record.candidate.base_branch == base_branch)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> Path:
        return self._write_model(
            "launchplane_merge_train_batch_landing_plans", record.record_id, record
        )

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainBatchLandingPlanRecord,
                "launchplane_merge_train_batch_landing_plans",
            )
            if (not repository or record.landing_plan.repository == repository)
            and (not base_branch or record.landing_plan.base_branch == base_branch)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_merge_train_stack_collapse_plan_record(
        self, record: MergeTrainStackCollapsePlanRecord
    ) -> Path:
        return self._write_model(
            "launchplane_merge_train_stack_collapse_plans", record.record_id, record
        )

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainStackCollapsePlanRecord,
                "launchplane_merge_train_stack_collapse_plans",
            )
            if (not repository or record.plan.repository == repository)
            and (not base_branch or record.plan.base_branch == base_branch)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_merge_train_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainPolicyRecord,
                "launchplane_merge_train_policies",
            )
            if not status or record.status == status
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_merge_train_run_record(self, run_id: str) -> MergeTrainRunRecord:
        return MergeTrainRunRecord.model_validate(
            self._read_model(
                MergeTrainRunRecord,
                "launchplane_merge_train_runs",
                run_id,
            ).model_dump(mode="json")
        )

    def latest_merge_train_run_record(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRunRecord | None:
        records = self.list_merge_train_run_records(
            repository=repository,
            base_branch=base_branch,
            limit=1,
        )
        return records[0] if records else None

    def list_merge_train_run_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        mode: str = "",
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[MergeTrainRunRecord, ...]:
        records = [
            record
            for record in self._list_models(
                MergeTrainRunRecord,
                "launchplane_merge_train_runs",
            )
            if (not repository or record.repository == repository)
            and (not base_branch or record.base_branch == base_branch)
            and (not mode or record.mode == mode)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.recorded_at, record.run_id), reverse=True)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                RuntimeKeySafetyPolicyRecord,
                "launchplane_runtime_key_safety_policies",
            )
            if not status or record.status == status
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_runner_host_hygiene_audit_records(
        self,
        *,
        host_name: str = "",
        action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerHostHygieneApplyAuditRecord, ...]:
        normalized_host_name = host_name.strip().lower()
        records = [
            record
            for record in self._list_models(
                RunnerHostHygieneApplyAuditRecord,
                "launchplane_runner_host_hygiene_audits",
            )
            if (
                not normalized_host_name
                or record.request.host_name.strip().lower() == normalized_host_name
            )
            and (not action or record.request.action == action)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: record.audit_record_key, reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_runner_host_hygiene_audit_record(
        self, audit_record_key: str
    ) -> RunnerHostHygieneApplyAuditRecord:
        return self._read_model(
            RunnerHostHygieneApplyAuditRecord,
            "launchplane_runner_host_hygiene_audits",
            _runner_host_hygiene_audit_record_id(audit_record_key),
        )

    def list_runner_lane_registration_audit_records(
        self,
        *,
        repository: str = "",
        host_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerLaneRegistrationAuditRecord, ...]:
        normalized_repository = repository.strip().lower()
        normalized_host_name = host_name.strip().lower()
        records = [
            record
            for record in self._list_models(
                RunnerLaneRegistrationAuditRecord,
                "launchplane_runner_lane_registration_audits",
            )
            if (
                not normalized_repository
                or record.request.repository.strip().lower() == normalized_repository
            )
            and (
                not normalized_host_name
                or record.request.host_name.strip().lower() == normalized_host_name
            )
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: record.audit_record_key, reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> Path:
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, record.request_id):
            return self._write_model(record_type, record.request_id, record)

    def create_every_code_work_request_record_if_absent(
        self, record: EveryCodeWorkRequestRecord
    ) -> tuple[EveryCodeWorkRequestRecord, bool]:
        created = self._create_model_if_absent(
            "launchplane_every_code_work_requests",
            record.request_id,
            record,
        )
        if created:
            return record, True
        return self.read_every_code_work_request_record(record.request_id), False

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        return EveryCodeWorkRequestRecord.model_validate(
            self._read_model(
                EveryCodeWorkRequestRecord,
                "launchplane_every_code_work_requests",
                request_id,
            ).model_dump(mode="json")
        )

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EveryCodeWorkRequestRecord,
                "launchplane_every_code_work_requests",
            )
            if (not state or record.state == state)
            and (not repository or record.repository == repository)
        ]
        records.sort(key=lambda record: (record.updated_at, record.request_id), reverse=True)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
        lease_seconds: int = 1800,
        idempotency_record_factory: (
            Callable[[EveryCodeWorkRequestRecord], LaunchplaneIdempotencyRecord] | None
        ) = None,
    ) -> EveryCodeWorkRequestRecord | None:
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, request_id):
            record = self.read_every_code_work_request_record(request_id)
            claimed_record = claim_every_code_work_request(
                record, host=host, claimed_at=claimed_at, lease_seconds=lease_seconds
            )
            if claimed_record is None:
                return None
            self._write_model(record_type, request_id, claimed_record)
            if idempotency_record_factory is not None:
                self.write_idempotency_record(idempotency_record_factory(claimed_record))
            return claimed_record

    def heartbeat_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        fencing_token: int,
        heartbeat_at: str,
        lease_expires_at: str,
        lease_seconds: int = 1800,
    ) -> bool:
        del lease_seconds
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, request_id):
            try:
                record = self.read_every_code_work_request_record(request_id)
            except FileNotFoundError:
                return False
            updated = heartbeat_every_code_work_request(
                record,
                EveryCodeWorkRequestHeartbeat(
                    host=host,
                    fencing_token=fencing_token,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                ),
            )
            if updated is None:
                return False
            self._write_model(record_type, request_id, updated)
            return True

    def update_every_code_work_request_status_record(
        self,
        *,
        request_id: str,
        update: EveryCodeWorkRequestStatusUpdate,
    ) -> EveryCodeWorkRequestRecord:
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, request_id):
            record = self.read_every_code_work_request_record(request_id)
            if record.fencing_token > 0 and update.fencing_token != record.fencing_token:
                raise ValueError(
                    f"Every Code status update fencing token {update.fencing_token} "
                    f"does not match record fencing token {record.fencing_token}"
                )
            updated = apply_every_code_work_request_status(record, update)
            self._write_model(record_type, request_id, updated)
            return updated

    def compare_and_write_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        record: EveryCodeWorkRequestRecord,
        idempotency_record: LaunchplaneIdempotencyRecord | None = None,
    ) -> Literal["updated", "changed", "missing"]:
        if expected_record.request_id != record.request_id:
            raise ValueError("Every Code compare-and-write requires matching request IDs")
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, expected_record.request_id):
            try:
                current = self.read_every_code_work_request_record(expected_record.request_id)
            except FileNotFoundError:
                return "missing"
            if current.model_dump(mode="json") != expected_record.model_dump(mode="json"):
                return "changed"
            self._write_model(record_type, record.request_id, record)
            if idempotency_record is not None:
                self.write_idempotency_record(idempotency_record)
            return "updated"

    def list_stale_every_code_work_request_records(
        self,
        *,
        as_of: str,
        limit: int = 50,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        stale_states = {"claimed", "running"}
        records = [
            record
            for record in self._list_models(
                EveryCodeWorkRequestRecord,
                "launchplane_every_code_work_requests",
            )
            if record.state in stale_states
            and record.lease_expires_at
            and record.lease_expires_at < as_of
        ]
        records.sort(key=lambda r: (r.lease_expires_at, r.request_id))
        return tuple(records[:limit])

    def recover_stale_every_code_work_request_record(
        self,
        *,
        expected_record: EveryCodeWorkRequestRecord,
        recovered_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        record_type = "launchplane_every_code_work_requests"
        with self._exclusive_record_lock(record_type, expected_record.request_id):
            try:
                current = self.read_every_code_work_request_record(expected_record.request_id)
            except FileNotFoundError:
                return None
            if current.model_dump(mode="json") != expected_record.model_dump(mode="json"):
                return None
            if not current.lease_expires_at or current.lease_expires_at >= recovered_at:
                return None
            recovered = recover_stale_every_code_work_request(
                current,
                recovered_at=recovered_at,
            )
            self._write_model(record_type, recovered.request_id, recovered)
            return recovered

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> Path:
        return self._write_model("launchplane_every_code_pr_feedback", record.feedback_id, record)

    def write_every_code_notification_policy_record(
        self, record: EveryCodeNotificationPolicyRecord
    ) -> Path:
        return self._write_model(
            "launchplane_every_code_notification_policies", record.policy_id, record
        )

    def list_every_code_notification_policy_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EveryCodeNotificationPolicyRecord,
                "launchplane_every_code_notification_policies",
            )
            if (not repository or record.repository in {"", repository})
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.policy_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_every_code_notification_attempt_record(
        self, record: EveryCodeNotificationAttemptRecord
    ) -> Path:
        return self._write_model(
            "launchplane_every_code_notification_attempts", record.attempt_id, record
        )

    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EveryCodeNotificationAttemptRecord,
                "launchplane_every_code_notification_attempts",
            )
            if (not request_id or record.request_id == request_id)
            and (not event or record.event == event)
            and (not destination_kind or record.destination_kind == destination_kind)
        ]
        records.sort(key=lambda record: (record.attempted_at, record.attempt_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_every_code_preview_gate_record(self, record: EveryCodePreviewGateRecord) -> Path:
        return self._write_model("launchplane_every_code_preview_gates", record.gate_id, record)

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EveryCodePreviewGateRecord,
                "launchplane_every_code_preview_gates",
            )
            if (not request_id or record.request_id == request_id)
            and (not repository or record.repository == repository)
            and (pr_number is None or record.pr_number == pr_number)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.gate_id), reverse=True)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_manager_preview_approval_event_record(
        self, record: ManagerPreviewApprovalEventRecord
    ) -> ManagerPreviewApprovalEventWriteStatus:
        record_type = "launchplane_manager_preview_approval_events"
        with self._product_authority_bundle_lock():
            record_path = self._record_path(record_type, record.event_id)
            if record_path.exists():
                existing = self._read_model_locked(
                    ManagerPreviewApprovalEventRecord,
                    record_type,
                    record.event_id,
                )
                if existing != record:
                    raise ManagerPreviewApprovalEventConflictError(
                        "Manager preview approval event replay changed the persisted payload."
                    )
                return "replayed"
            self._write_model_locked(record_type, record.event_id, record)
            return "written"

    def list_manager_preview_approval_event_records(
        self,
        *,
        product: str = "",
        context: str = "",
        repository: str = "",
        pr_number: int | None = None,
        preview_id: str = "",
        action: str = "",
        limit: int | None = None,
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]:
        records = [
            record
            for record in self._list_models(
                ManagerPreviewApprovalEventRecord,
                "launchplane_manager_preview_approval_events",
            )
            if (not product or record.binding.product == product.lower())
            and (not context or record.binding.context == context.lower())
            and (not repository or record.binding.repository == repository.lower())
            and (pr_number is None or record.binding.pr_number == pr_number)
            and (not preview_id or record.binding.preview_id == preview_id)
            and (not action or record.action == action)
        ]
        records.sort(key=lambda record: (record.occurred_at, record.event_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EveryCodePrFeedbackRecord,
                "launchplane_every_code_pr_feedback",
            )
            if (not request_id or record.request_id == request_id)
            and (not repository or record.repository == repository)
            and (pr_number is None or record.pr_number == pr_number)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.received_at, record.feedback_id), reverse=True)
        if offset > 0:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> Path:
        return self._write_model("launchplane_product_profiles", record.product, record)

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> Path:
        return self._write_model(
            "dokploy_target_ids",
            _context_instance_record_id(record.context, record.instance),
            record,
        )

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        return self._read_model(
            DokployTargetIdRecord,
            "dokploy_target_ids",
            _context_instance_record_id(context_name, instance_name),
        )

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        records = list(self._list_models(DokployTargetIdRecord, "dokploy_target_ids"))
        records.sort(key=lambda record: (record.context, record.instance))
        return tuple(records)

    def delete_dokploy_target_id_record(
        self,
        *,
        expected_record: DokployTargetIdRecord,
    ) -> CurrentAuthorityDeleteStatus:
        return self._delete_expected_authority_record(
            record_type="dokploy_target_ids",
            record_id=_context_instance_record_id(
                expected_record.context,
                expected_record.instance,
            ),
            expected_record=expected_record,
        )

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> Path:
        return self._write_model(
            "dokploy_targets",
            _context_instance_record_id(record.context, record.instance),
            record,
        )

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        return self._read_model(
            DokployTargetRecord,
            "dokploy_targets",
            _context_instance_record_id(context_name, instance_name),
        )

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
        records = list(self._list_models(DokployTargetRecord, "dokploy_targets"))
        records.sort(key=lambda record: (record.context, record.instance))
        return tuple(records)

    def delete_dokploy_target_record(
        self,
        *,
        expected_record: DokployTargetRecord,
    ) -> CurrentAuthorityDeleteStatus:
        return self._delete_expected_authority_record(
            record_type="dokploy_targets",
            record_id=_context_instance_record_id(
                expected_record.context,
                expected_record.instance,
            ),
            expected_record=expected_record,
        )

    def write_provider_target_record(self, record: ProviderTargetRecord) -> Path:
        return self._write_model(
            "launchplane_provider_targets",
            _context_instance_record_id(record.context, record.instance),
            record,
        )

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        return self._read_model(
            ProviderTargetRecord,
            "launchplane_provider_targets",
            _context_instance_record_id(context_name, instance_name),
        )

    def list_provider_target_records(
        self, *, provider_id: str = ""
    ) -> tuple[ProviderTargetRecord, ...]:
        normalized_provider_id = provider_id.strip().lower()
        records = [
            record
            for record in self._list_models(
                ProviderTargetRecord,
                "launchplane_provider_targets",
            )
            if not normalized_provider_id or record.provider_id == normalized_provider_id
        ]
        records.sort(key=lambda record: (record.context, record.instance))
        return tuple(records)

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]:
        return self.list_provider_target_records()

    def create_provider_target_record_if_absent(
        self, record: ProviderTargetRecord
    ) -> ProviderTargetCreateStatus:
        created = self._create_model_if_absent(
            "launchplane_provider_targets",
            _context_instance_record_id(record.context, record.instance),
            record,
        )
        return "created" if created else "exists"

    def delete_provider_target_record(
        self,
        *,
        expected_record: ProviderTargetRecord,
    ) -> CurrentAuthorityDeleteStatus:
        return self._delete_expected_authority_record(
            record_type="launchplane_provider_targets",
            record_id=_context_instance_record_id(
                expected_record.context,
                expected_record.instance,
            ),
            expected_record=expected_record,
        )

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> Path:
        return self._write_model(
            "launchplane_runtime_environments",
            _runtime_environment_record_id(record),
            record,
        )

    def delete_runtime_environment_record_with_event(
        self,
        *,
        event: RuntimeEnvironmentDeleteEvent,
        expected_record: RuntimeEnvironmentRecord,
    ) -> RuntimeEnvironmentDeleteStatus:
        bundle = ProductAuthorityBundle(
            delete_runtime_environments=(
                RuntimeEnvironmentDelete(expected_record=expected_record, event=event),
            )
        )
        try:
            self.write_product_authority_bundle(bundle)
        except FileNotFoundError:
            return "missing"
        except ValueError:
            return "changed"
        return "deleted"

    def write_runtime_environment_delete_event(self, event: RuntimeEnvironmentDeleteEvent) -> Path:
        return self._write_model(
            "launchplane_runtime_environment_delete_events",
            event.event_id,
            event,
        )

    def list_runtime_environment_delete_events(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentDeleteEvent, ...]:
        records = [
            record
            for record in self._list_models(
                RuntimeEnvironmentDeleteEvent,
                "launchplane_runtime_environment_delete_events",
            )
            if (not scope or record.scope == scope)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.recorded_at, record.event_id), reverse=True)
        return tuple(records)

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        records = [
            record
            for record in self._list_models(
                RuntimeEnvironmentRecord,
                "launchplane_runtime_environments",
            )
            if (not scope or record.scope == scope)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.scope, record.context, record.instance))
        return tuple(records)

    def write_secret_record(self, record: SecretRecord) -> Path:
        return self._write_model("launchplane_secrets", record.secret_id, record)

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        return self._read_model(SecretRecord, "launchplane_secrets", secret_id)

    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None:
        records = [
            record
            for record in self._list_models(SecretRecord, "launchplane_secrets")
            if record.scope == scope
            and record.integration == integration
            and record.name == name
            and record.context == context
            and record.instance == instance
        ]
        records.sort(key=lambda record: (record.updated_at, record.secret_id), reverse=True)
        return records[0] if records else None

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        records = [
            record
            for record in self._list_models(SecretRecord, "launchplane_secrets")
            if (not integration or record.integration == integration)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.updated_at, record.secret_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_secret_version(self, version: SecretVersion) -> Path:
        return self._write_model("launchplane_secret_versions", version.version_id, version)

    def read_secret_version(self, version_id: str) -> SecretVersion:
        return self._read_model(SecretVersion, "launchplane_secret_versions", version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        records = [
            version
            for version in self._list_models(SecretVersion, "launchplane_secret_versions")
            if version.secret_id == secret_id
        ]
        records.sort(key=lambda version: (version.created_at, version.version_id), reverse=True)
        return tuple(records)

    def write_secret_binding(self, binding: SecretBinding) -> Path:
        return self._write_model("launchplane_secret_bindings", binding.binding_id, binding)

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        records = [
            binding
            for binding in self._list_models(SecretBinding, "launchplane_secret_bindings")
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        ]
        records.sort(key=lambda binding: (binding.updated_at, binding.binding_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_secret_audit_event(self, event: SecretAuditEvent) -> Path:
        return self._write_model("launchplane_secret_audit_events", event.event_id, event)

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        records = [
            event
            for event in self._list_models(
                SecretAuditEvent,
                "launchplane_secret_audit_events",
            )
            if event.secret_id == secret_id
        ]
        records.sort(key=lambda event: (event.recorded_at, event.event_id), reverse=True)
        return tuple(records)

    def write_secret_rotations(
        self,
        rotations: tuple[SecretRotationWrite, ...],
        *,
        idempotency_record: LaunchplaneIdempotencyRecord | None = None,
    ) -> None:
        ordered_rotations = tuple(sorted(rotations, key=lambda item: item.record.secret_id))
        for rotation in ordered_rotations:
            current_record = self.read_secret_record(rotation.record.secret_id)
            if current_record.current_version_id != rotation.expected_current_version_id:
                raise ValueError("Managed secret changed after rotation preflight.")
        self.write_product_authority_bundle(
            ProductAuthorityBundle(
                secret_versions=tuple(rotation.version for rotation in ordered_rotations),
                secret_records=tuple(rotation.record for rotation in ordered_rotations),
                secret_audit_events=tuple(rotation.audit_event for rotation in ordered_rotations),
                idempotency_record=idempotency_record,
            )
        )

    def _delete_expected_authority_record(
        self,
        *,
        record_type: str,
        record_id: str,
        expected_record: BaseModel,
    ) -> CurrentAuthorityDeleteStatus:
        with self._product_authority_bundle_lock():
            return self._delete_expected_authority_record_locked(
                record_type=record_type,
                record_id=record_id,
                expected_record=expected_record,
            )

    def _delete_expected_authority_record_locked(
        self,
        *,
        record_type: str,
        record_id: str,
        expected_record: BaseModel,
    ) -> CurrentAuthorityDeleteStatus:
        record_path = self._record_path(record_type, record_id)
        current_payload = self._read_json_file(record_path)
        if current_payload is None:
            return "missing"
        expected_payload = expected_record.model_dump(mode="json", exclude_none=True)
        if current_payload != expected_payload:
            return "changed"
        record_path.unlink()
        return "deleted"

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> Path:
        return self._write_model(
            "launchplane_public_ingress_observations", record.record_id, record
        )

    def write_ingress_route_audit_record(self, record: IngressRouteAuditRecord) -> Path:
        return self._write_model("launchplane_ingress_route_audits", record.record_id, record)

    def write_ingress_canary_route_record(self, record: IngressCanaryRouteRecord) -> Path:
        return self._write_model(
            "launchplane_ingress_canary_routes",
            _ingress_canary_route_record_id(record.canary_key),
            record,
        )

    def write_route_binding_record(self, record: EnvironmentRouteBindingRecord) -> Path:
        return self._write_model(
            "launchplane_route_bindings",
            _route_binding_record_id(record.binding_key),
            record,
        )

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord:
        return self._read_model(
            EnvironmentRouteBindingRecord,
            "launchplane_route_bindings",
            _route_binding_record_id(
                build_route_binding_key(
                    product=product,
                    context=context_name,
                    instance=instance_name,
                )
            ),
        )

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EnvironmentRouteBindingRecord,
                "launchplane_route_bindings",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.product, record.context, record.instance))
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord:
        return self._read_model(
            IngressCanaryRouteRecord,
            "launchplane_ingress_canary_routes",
            _ingress_canary_route_record_id(canary_key),
        )

    def list_ingress_canary_route_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[IngressCanaryRouteRecord, ...]:
        records = [
            record
            for record in self._list_models(
                IngressCanaryRouteRecord,
                "launchplane_ingress_canary_routes",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.product, record.context, record.canary_key))
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_trusted_maintenance_policies"
        with self._product_authority_bundle_lock():
            records = self._list_models_locked(TrustedMaintenancePolicyRecord, record_type)
            plan = plan_trusted_maintenance_policy_append(records=records, record=record)
            if plan.status == "replayed":
                return "replayed"
            if plan.superseded_current_record is None:
                self._write_model_locked(record_type, record.record_id, record)
                return "written"
            self._write_trusted_maintenance_policy_replacement_locked(
                record_type=record_type,
                superseded_current_record=plan.superseded_current_record,
                active_record=record,
            )
            return "written"

    def compare_and_write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_trusted_maintenance_policies"
        with self._product_authority_bundle_lock():
            records = self._list_models_locked(TrustedMaintenancePolicyRecord, record_type)
            plan = plan_trusted_maintenance_policy_apply(
                records=records,
                record=record,
                expected_current_record_id=expected_current_record_id,
                expected_current_policy_digest=expected_current_policy_digest,
            )
            if plan.status == "replayed":
                return "replayed"
            if plan.superseded_current_record is None:
                self._write_model_locked(record_type, record.record_id, record)
                return "written"
            self._write_trusted_maintenance_policy_replacement_locked(
                record_type=record_type,
                superseded_current_record=plan.superseded_current_record,
                active_record=record,
            )
            return "written"

    def _write_trusted_maintenance_policy_replacement_locked(
        self,
        *,
        record_type: str,
        superseded_current_record: TrustedMaintenancePolicyRecord,
        active_record: TrustedMaintenancePolicyRecord,
    ) -> None:
        stage_id = f"{_utc_now_timestamp().replace(':', '').replace('-', '')}-{time.time_ns()}"
        stage_dir = self._product_authority_bundle_stage_root() / stage_id
        records_dir = stage_dir / "records"
        records_dir.mkdir(parents=True, exist_ok=False)
        entries: list[_AuthorityBundleStageEntry] = []
        try:
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type=record_type,
                record_id=superseded_current_record.record_id,
                model=superseded_current_record,
                step_name="supersede_trusted_maintenance_policy",
            )
            self._stage_product_authority_bundle_write(
                stage_dir=stage_dir,
                entries=entries,
                record_type=record_type,
                record_id=active_record.record_id,
                model=active_record,
                step_name="write_trusted_maintenance_policy",
            )
            manifest = _AuthorityBundleStageManifest(
                stage_id=stage_id,
                state="ready",
                entries=tuple(entries),
            )
            self._write_product_authority_bundle_stage_manifest(
                stage_dir=stage_dir,
                manifest=manifest,
            )
            self._after_product_authority_bundle_step(
                "stage_trusted_maintenance_policy_replacement"
            )
            publishing_manifest = manifest.model_copy(update={"state": "publishing"})
            self._write_product_authority_bundle_stage_manifest(
                stage_dir=stage_dir,
                manifest=publishing_manifest,
            )
            self._after_product_authority_bundle_step(
                "publish_trusted_maintenance_policy_replacement"
            )
            self._publish_product_authority_bundle_stage(
                manifest=publishing_manifest,
                stage_dir=stage_dir,
                recovering=False,
            )
        except Exception:
            if not (stage_dir / "manifest.json").exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def read_trusted_maintenance_policy_record(
        self,
        record_id: str,
    ) -> TrustedMaintenancePolicyRecord:
        return self._read_model(
            TrustedMaintenancePolicyRecord,
            "launchplane_trusted_maintenance_policies",
            record_id,
        )

    def list_trusted_maintenance_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenancePolicyRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = [
            record
            for record in self._list_models(
                TrustedMaintenancePolicyRecord,
                "launchplane_trusted_maintenance_policies",
            )
            if (not repository_id or record.repository_id == repository_id)
            and (not repository_owner_id or record.repository_owner_id == repository_owner_id)
            and (not normalized_repository or record.repository == normalized_repository)
            and (not product or record.product == product)
            and (not context or record.context == context)
            and (not status or record.status == status)
        ]
        records.sort(
            key=lambda record: (
                record.policy_revision,
                record.repository_id,
                record.product,
                record.context,
                record.record_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_trusted_maintenance_evidence_record(
        self,
        record: TrustedMaintenanceEvidenceRecord,
    ) -> Literal["written", "replayed"]:
        record_type = "launchplane_trusted_maintenance_evidence"
        with self._product_authority_bundle_lock():
            records = self._list_models_locked(
                TrustedMaintenanceEvidenceRecord,
                record_type,
            )
            plan = plan_trusted_maintenance_evidence_append(records=records, record=record)
            if plan.status == "replayed":
                return "replayed"
            self._write_model_locked(record_type, record.evidence_id, record)
            return "written"

    def read_trusted_maintenance_evidence_record(
        self,
        evidence_id: str,
    ) -> TrustedMaintenanceEvidenceRecord:
        return self._read_model(
            TrustedMaintenanceEvidenceRecord,
            "launchplane_trusted_maintenance_evidence",
            evidence_id,
        )

    def list_trusted_maintenance_evidence_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        evidence_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        policy_record_id: str = "",
        policy_digest: str = "",
        matched_actor_rule_id: str = "",
        pr_author_github_id: int | None = None,
        sender_github_id: int | None = None,
        event_name: str = "",
        event_action: str = "",
        delivery_id: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenanceEvidenceRecord, ...]:
        normalized_repository = repository.strip().lower()
        records = [
            record
            for record in self._list_models(
                TrustedMaintenanceEvidenceRecord,
                "launchplane_trusted_maintenance_evidence",
            )
            if (not repository_id or record.binding.repository_id == repository_id)
            and (
                not repository_owner_id or record.binding.repository_owner_id == repository_owner_id
            )
            and (not normalized_repository or record.binding.repository == normalized_repository)
            and (not product or record.binding.product == product)
            and (not context or record.binding.context == context)
            and (not evidence_id or record.evidence_id == evidence_id)
            and (not binding_sha256 or record.binding.binding_sha256 == binding_sha256)
            and (
                pull_request_number is None
                or record.binding.pull_request_number == pull_request_number
            )
            and (not head_sha or record.binding.head_sha == head_sha)
            and (
                not classification_digest
                or record.binding.classification_digest == classification_digest
            )
            and (not policy_record_id or record.binding.policy_record_id == policy_record_id)
            and (not policy_digest or record.binding.policy_digest == policy_digest)
            and (
                not matched_actor_rule_id
                or record.binding.matched_actor_rule_id == matched_actor_rule_id
            )
            and (
                pr_author_github_id is None
                or record.binding.pr_author_github_id == pr_author_github_id
            )
            and (sender_github_id is None or record.binding.sender_github_id == sender_github_id)
            and (not event_name or record.binding.event_name == event_name)
            and (not event_action or record.binding.event_action == event_action)
            and (not delivery_id or record.binding.delivery_id == delivery_id)
        ]
        records.sort(
            key=lambda record: (
                record.occurred_at,
                record.evidence_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_edge_endpoint_record(self, record: EdgeEndpointRecord) -> Path:
        return self._write_model(
            "launchplane_edge_endpoints",
            _edge_endpoint_record_id(record.endpoint_key),
            record,
        )

    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord:
        return self._read_model(
            EdgeEndpointRecord,
            "launchplane_edge_endpoints",
            _edge_endpoint_record_id(endpoint_key),
        )

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]:
        records = [
            record
            for record in self._list_models(
                EdgeEndpointRecord,
                "launchplane_edge_endpoints",
            )
            if (not provider or record.provider == provider)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.provider, record.endpoint_key))
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_private_health_endpoint_record(self, record: PrivateHealthEndpointRecord) -> Path:
        return self._write_model(
            "launchplane_private_health_endpoints",
            _private_health_endpoint_record_id(record.endpoint_key),
            record,
        )

    def read_private_health_endpoint_record(self, endpoint_key: str) -> PrivateHealthEndpointRecord:
        return self._read_model(
            PrivateHealthEndpointRecord,
            "launchplane_private_health_endpoints",
            _private_health_endpoint_record_id(endpoint_key),
        )

    def list_private_health_endpoint_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PrivateHealthEndpointRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PrivateHealthEndpointRecord,
                "launchplane_private_health_endpoints",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.product, record.context, record.instance))
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_ingress_route_audit_record(self, record_id: str) -> IngressRouteAuditRecord:
        return self._read_model(
            IngressRouteAuditRecord,
            "launchplane_ingress_route_audits",
            record_id,
        )

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]:
        records = [
            record
            for record in self._list_models(
                IngressRouteAuditRecord,
                "launchplane_ingress_route_audits",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
        ]
        records.sort(key=lambda record: (record.recorded_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        incident_id: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressObservationRecord,
                "launchplane_public_ingress_observations",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or record.check_kind == check_kind)
            and (not incident_id or record.incident_id == incident_id)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_incident_record(self, record: PublicIngressIncidentRecord) -> Path:
        return self._write_model("launchplane_public_ingress_incidents", record.incident_id, record)

    def read_public_ingress_incident_record(self, incident_id: str) -> PublicIngressIncidentRecord:
        for record in self.list_public_ingress_incident_records(limit=None):
            if record.incident_id == incident_id:
                return record
        raise FileNotFoundError(f"Public ingress incident '{incident_id}' was not found.")

    def write_public_ingress_incident_event_record(
        self, record: PublicIngressIncidentEventRecord
    ) -> Path:
        return self._write_model(
            "launchplane_public_ingress_incident_events", record.event_id, record
        )

    def list_public_ingress_incident_event_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentEventRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressIncidentEventRecord,
                "launchplane_public_ingress_incident_events",
            )
            if (not incident_id or record.incident_id == incident_id)
            and (not event or record.event == event)
        ]
        records.sort(key=lambda record: (record.occurred_at, record.event_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_incident_reminder_state_record(
        self, record: PublicIngressIncidentReminderStateRecord
    ) -> Path:
        return self._write_model(
            "launchplane_public_ingress_incident_reminders",
            record.reminder_state_id,
            record,
        )

    def list_public_ingress_incident_reminder_state_records(
        self,
        *,
        incident_id: str = "",
        policy_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressIncidentReminderStateRecord,
                "launchplane_public_ingress_incident_reminders",
            )
            if (not incident_id or record.incident_id == incident_id)
            and (not policy_id or record.policy_id == policy_id)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.reminder_state_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressIncidentRecord,
                "launchplane_public_ingress_incidents",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or record.check_kind == check_kind)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.opened_at, record.incident_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_notification_policy_record(
        self, record: PublicIngressNotificationPolicyRecord
    ) -> Path:
        return self._write_model(
            "launchplane_public_ingress_notification_policies", record.policy_id, record
        )

    def list_public_ingress_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressNotificationPolicyRecord,
                "launchplane_public_ingress_notification_policies",
            )
            if (not product or record.product in {"", product})
            and (not context_name or record.context in {"", context_name})
            and (not instance_name or record.instance in {"", instance_name})
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.policy_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_notification_attempt_record(
        self, record: PublicIngressNotificationAttemptRecord
    ) -> Path:
        return self._write_model(
            "launchplane_public_ingress_notification_attempts", record.attempt_id, record
        )

    def list_public_ingress_notification_attempt_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PublicIngressNotificationAttemptRecord,
                "launchplane_public_ingress_notification_attempts",
            )
            if (not incident_id or record.incident_id == incident_id)
            and (not event or record.event == event)
            and (not destination_kind or record.destination_kind == destination_kind)
        ]
        records.sort(key=lambda record: (record.attempted_at, record.attempt_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def list_outbox_delivery_records(
        self,
        *,
        states: tuple[str, ...] = (),
        kind: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
        limit: int | None = None,
    ) -> tuple[OutboxDeliveryRecord, ...]:
        records = [
            record
            for record in self._list_models(
                OutboxDeliveryRecord,
                "launchplane_outbox_deliveries",
            )
            if (not states or record.state in states)
            and (not kind or record.kind == kind)
            and (not aggregate_type or record.aggregate_type == aggregate_type)
            and (not aggregate_id or record.aggregate_id == aggregate_id)
        ]
        records.sort(key=lambda record: (record.created_at, record.delivery_id))
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        with self._product_authority_bundle_lock():
            return self._read_product_profile_record_path(
                self._record_path("launchplane_product_profiles", product)
            )

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        with self._product_authority_bundle_lock():
            record_dir = self._record_dir("launchplane_product_profiles")
            records: list[LaunchplaneProductProfileRecord] = []
            if record_dir.exists():
                for record_path in sorted(record_dir.glob("*.json")):
                    record = self._read_product_profile_record_path(record_path)
                    if not driver_id or record.driver_id == driver_id:
                        records.append(record)
            records.sort(key=lambda record: record.product)
            return tuple(records)

    def _read_product_profile_record_path(
        self, record_path: Path
    ) -> LaunchplaneProductProfileRecord:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        migrated_payload = migrate_product_profile_monitoring_intent_payload(
            migrate_product_profile_health_monitoring_payload(payload)
        )
        record = LaunchplaneProductProfileRecord.model_validate(migrated_payload)
        if migrated_payload != payload:
            record_path.write_text(
                json.dumps(
                    record.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True
                ),
                encoding="utf-8",
            )
        return record

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                LaunchplaneAuthzPolicyRecord, "launchplane_authz_policies"
            )
            if not status or record.status == status
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord:
        return ReleaseTupleRecord.model_validate(
            self._read_model(
                ReleaseTupleRecord,
                "release_tuples",
                f"{context_name}-{channel_name}",
            ).model_dump(mode="json")
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        records = list(self._list_models(ReleaseTupleRecord, "release_tuples"))
        records.sort(key=lambda record: (record.context, record.channel))
        return tuple(records)

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> Path:
        return self._write_model("idempotency", record.record_id, record)

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        for record in self._list_models(LaunchplaneIdempotencyRecord, "idempotency"):
            if (
                record.scope == scope
                and record.route_path == route_path
                and record.idempotency_key == idempotency_key
            ):
                return record
        return None

    def write_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> Path:
        return self._write_model("odoo_stable_bootstrap_operations", record.operation_id, record)

    def create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]:
        reservation_id = _odoo_stable_lane_reservation_id(
            product=record.product,
            context=record.context,
            instance=record.instance,
        )
        with self._exclusive_record_lock("odoo_stable_lane_operation_reservations", reservation_id):
            active_operation = self._active_odoo_stable_lane_operation(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_operation is not None:
                if isinstance(active_operation, OdooStableBootstrapOperationRecord):
                    return active_operation, False
                raise OdooStableLaneOperationConflictError(
                    _odoo_stable_lane_operation_owner(active_operation)
                )
            return self._create_odoo_stable_bootstrap_operation_record(record)

    def _create_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> tuple[OdooStableBootstrapOperationRecord, bool]:
        reservation_id = _odoo_stable_bootstrap_lane_reservation_id(record)
        reservation_path = self._record_path(
            "odoo_stable_bootstrap_lane_reservations", reservation_id
        )
        reservation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with reservation_path.open("x", encoding="utf-8") as reservation_file:
                reservation_file.write(record.operation_id)
                reservation_file.flush()
                os.fsync(reservation_file.fileno())
        except FileExistsError:
            stale_reservation_deadline = (
                time.monotonic() + self.odoo_stable_bootstrap_reservation_settle_timeout_seconds
            )
            reserved_operation_id = self._wait_for_odoo_stable_bootstrap_reservation_owner(
                reservation_path, stale_reservation_deadline
            )
            if reserved_operation_id:
                owner_record_deadline = (
                    time.monotonic() + self.odoo_stable_bootstrap_reservation_settle_timeout_seconds
                )
                reserved_operation = self._wait_for_odoo_stable_bootstrap_reserved_operation(
                    reserved_operation_id, owner_record_deadline
                )
                if (
                    reserved_operation is not None
                    and reserved_operation.status in ODOO_STABLE_LANE_BLOCKING_STATUSES
                ):
                    return reserved_operation, False
            reservation_path.unlink(missing_ok=True)
            return self._create_odoo_stable_bootstrap_operation_record(record)
        self.write_odoo_stable_bootstrap_operation_record(record)
        return record, True

    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord:
        return OdooStableBootstrapOperationRecord.model_validate(
            self._read_model(
                OdooStableBootstrapOperationRecord,
                "odoo_stable_bootstrap_operations",
                operation_id,
            ).model_dump(mode="json")
        )

    def list_odoo_stable_bootstrap_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableBootstrapOperationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                OdooStableBootstrapOperationRecord,
                "odoo_stable_bootstrap_operations",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not idempotency_key or record.idempotency_key == idempotency_key)
            and (not statuses or record.status in statuses)
        ]
        records.sort(key=lambda record: (record.updated_at, record.operation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def cancel_pending_odoo_stable_bootstrap_operation_record(
        self, record: OdooStableBootstrapOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo stable bootstrap cancellation requires a cancelled record.")
        with self._exclusive_record_lock("odoo_stable_bootstrap_operations", record.operation_id):
            current_record = self.read_odoo_stable_bootstrap_operation_record(record.operation_id)
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self.write_odoo_stable_bootstrap_operation_record(record)
            return True

    def claim_next_odoo_stable_bootstrap_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableBootstrapOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Odoo stable bootstrap operation claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("Odoo stable bootstrap operation claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("Odoo stable bootstrap operation claim requires claimed_at.")
        pending_records = self.list_odoo_stable_bootstrap_operation_records(statuses=("pending",))
        if not pending_records:
            return None
        for record in sorted(
            pending_records,
            key=lambda item: (item.created_at, item.operation_id),
        ):
            reservation_id = _odoo_stable_lane_reservation_id(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            with self._exclusive_record_lock(
                "odoo_stable_lane_operation_reservations",
                reservation_id,
            ):
                active_operation = self._active_odoo_stable_lane_operation(
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_operation is None or _odoo_stable_lane_operation_owner(
                    active_operation
                ) != OdooStableLaneOperationOwner(
                    operation_kind="stable_bootstrap",
                    operation_id=record.operation_id,
                ):
                    continue
                with self._exclusive_record_lock(
                    "odoo_stable_bootstrap_operations", record.operation_id
                ):
                    current_record = self.read_odoo_stable_bootstrap_operation_record(
                        record.operation_id
                    )
                    if current_record.status != "pending":
                        continue
                    claimed_record = current_record.model_copy(
                        update={
                            "status": "running",
                            "phase": "running",
                            "started_at": current_record.started_at or claimed_at,
                            "updated_at": claimed_at,
                            "lease_owner": normalized_lease_owner,
                            "lease_expires_at": lease_expires_at.strip(),
                            "heartbeat_at": claimed_at.strip(),
                            "attempt": current_record.attempt + 1,
                        }
                    )
                    self.write_odoo_stable_bootstrap_operation_record(claimed_record)
                    return claimed_record
        return None

    def heartbeat_odoo_stable_bootstrap_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._exclusive_record_lock("odoo_stable_bootstrap_operations", operation_id):
            record = self.read_odoo_stable_bootstrap_operation_record(operation_id)
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self.write_odoo_stable_bootstrap_operation_record(heartbeat_record)
            return True

    def complete_odoo_stable_bootstrap_operation_record(
        self,
        *,
        record: OdooStableBootstrapOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._exclusive_record_lock("odoo_stable_bootstrap_operations", record.operation_id):
            current_record = self.read_odoo_stable_bootstrap_operation_record(record.operation_id)
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self.write_odoo_stable_bootstrap_operation_record(record)
            return True

    def recover_expired_odoo_stable_bootstrap_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        affected_operation_ids: list[str] = []
        candidates = self.list_odoo_stable_bootstrap_operation_records(statuses=("running",))
        for candidate in candidates:
            with self._exclusive_record_lock(
                "odoo_stable_bootstrap_operations", candidate.operation_id
            ):
                record = self.read_odoo_stable_bootstrap_operation_record(candidate.operation_id)
                if record.status != "running" or (
                    record.lease_expires_at and record.lease_expires_at >= now
                ):
                    continue
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo stable bootstrap operation exhausted automatic attempts in "
                                f"safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                f"Odoo stable bootstrap operation lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self.write_odoo_stable_bootstrap_operation_record(recovered_record)
        return tuple(affected_operation_ids)

    def _wait_for_odoo_stable_bootstrap_reservation_owner(
        self,
        reservation_path: Path,
        deadline: float,
    ) -> str:
        while True:
            try:
                reserved_operation_id = reservation_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return ""
            if reserved_operation_id:
                return reserved_operation_id
            if time.monotonic() >= deadline:
                return ""
            time.sleep(self.odoo_stable_bootstrap_reservation_poll_seconds)

    def _wait_for_odoo_stable_bootstrap_reserved_operation(
        self, operation_id: str, deadline: float
    ) -> OdooStableBootstrapOperationRecord | None:
        while True:
            try:
                return self.read_odoo_stable_bootstrap_operation_record(operation_id)
            except (FileNotFoundError, JSONDecodeError):
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.odoo_stable_bootstrap_reservation_poll_seconds)

    def write_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> Path:
        return self._write_model(
            "odoo_stable_target_replacement_operations", record.operation_id, record
        )

    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord:
        return OdooStableTargetReplacementOperationRecord.model_validate(
            self._read_model(
                OdooStableTargetReplacementOperationRecord,
                "odoo_stable_target_replacement_operations",
                operation_id,
            ).model_dump(mode="json")
        )

    def list_odoo_stable_target_replacement_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                OdooStableTargetReplacementOperationRecord,
                "odoo_stable_target_replacement_operations",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not idempotency_key or record.idempotency_key == idempotency_key)
            and (not idempotency_scope or record.idempotency_scope == idempotency_scope)
            and (not statuses or record.status in statuses)
        ]
        records.sort(key=lambda record: (record.updated_at, record.operation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def cancel_pending_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo target replacement cancellation requires a cancelled record.")
        with self._exclusive_record_lock(
            "odoo_stable_target_replacement_operations", record.operation_id
        ):
            current_record = self.read_odoo_stable_target_replacement_operation_record(
                record.operation_id
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self.write_odoo_stable_target_replacement_operation_record(record)
            return True

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
        reservation_id = _odoo_stable_lane_reservation_id(
            product=record.product,
            context=record.context,
            instance=record.instance,
        )
        with self._exclusive_record_lock("odoo_stable_lane_operation_reservations", reservation_id):
            active_operation = self._active_odoo_stable_lane_operation(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_operation is not None:
                if isinstance(active_operation, OdooStableTargetReplacementOperationRecord):
                    return active_operation, False
                raise OdooStableLaneOperationConflictError(
                    _odoo_stable_lane_operation_owner(active_operation)
                )
            return self._create_odoo_stable_target_replacement_operation_record(record)

    def _create_odoo_stable_target_replacement_operation_record(
        self, record: OdooStableTargetReplacementOperationRecord
    ) -> tuple[OdooStableTargetReplacementOperationRecord, bool]:
        reservation_id = _odoo_target_replacement_lane_reservation_id(record)
        reservation_path = self._record_path(
            "odoo_stable_target_replacement_lane_reservations", reservation_id
        )
        reservation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with reservation_path.open("x", encoding="utf-8") as reservation_file:
                reservation_file.write(record.operation_id)
                reservation_file.flush()
                os.fsync(reservation_file.fileno())
        except FileExistsError:
            stale_reservation_deadline = (
                time.monotonic() + self.odoo_target_replacement_reservation_settle_timeout_seconds
            )
            reserved_operation_id = self._wait_for_odoo_stable_target_replacement_reservation_owner(
                reservation_path, stale_reservation_deadline
            )
            if reserved_operation_id:
                owner_record_deadline = (
                    time.monotonic()
                    + self.odoo_target_replacement_reservation_settle_timeout_seconds
                )
                reserved_operation = (
                    self._wait_for_odoo_stable_target_replacement_reserved_operation(
                        reserved_operation_id, owner_record_deadline
                    )
                )
                if (
                    reserved_operation is not None
                    and reserved_operation.status in ODOO_STABLE_LANE_BLOCKING_STATUSES
                ):
                    return reserved_operation, False
            reservation_path.unlink(missing_ok=True)
            return self._create_odoo_stable_target_replacement_operation_record(record)
        self.write_odoo_stable_target_replacement_operation_record(record)
        return record, True

    def claim_next_odoo_stable_target_replacement_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableTargetReplacementOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("Odoo stable target replacement claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("Odoo stable target replacement claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("Odoo stable target replacement claim requires claimed_at.")
        pending_records = self.list_odoo_stable_target_replacement_operation_records(
            statuses=("pending",)
        )
        if not pending_records:
            return None
        for record in sorted(
            pending_records,
            key=lambda item: (item.created_at, item.operation_id),
        ):
            reservation_id = _odoo_stable_lane_reservation_id(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            with self._exclusive_record_lock(
                "odoo_stable_lane_operation_reservations",
                reservation_id,
            ):
                active_operation = self._active_odoo_stable_lane_operation(
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_operation is None or _odoo_stable_lane_operation_owner(
                    active_operation
                ) != OdooStableLaneOperationOwner(
                    operation_kind="target_replacement",
                    operation_id=record.operation_id,
                ):
                    continue
                with self._exclusive_record_lock(
                    "odoo_stable_target_replacement_operations", record.operation_id
                ):
                    current_record = self.read_odoo_stable_target_replacement_operation_record(
                        record.operation_id
                    )
                    if current_record.status != "pending":
                        continue
                    claimed_record = current_record.model_copy(
                        update={
                            "status": "running",
                            "phase": "running",
                            "started_at": current_record.started_at or claimed_at,
                            "updated_at": claimed_at,
                            "lease_owner": normalized_lease_owner,
                            "lease_expires_at": lease_expires_at.strip(),
                            "heartbeat_at": claimed_at.strip(),
                            "attempt": current_record.attempt + 1,
                        }
                    )
                    self.write_odoo_stable_target_replacement_operation_record(claimed_record)
                    return claimed_record
        return None

    def heartbeat_odoo_stable_target_replacement_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._exclusive_record_lock("odoo_stable_target_replacement_operations", operation_id):
            record = self.read_odoo_stable_target_replacement_operation_record(operation_id)
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self.write_odoo_stable_target_replacement_operation_record(heartbeat_record)
            return True

    def complete_odoo_stable_target_replacement_operation_record(
        self,
        *,
        record: OdooStableTargetReplacementOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._exclusive_record_lock(
            "odoo_stable_target_replacement_operations", record.operation_id
        ):
            current_record = self.read_odoo_stable_target_replacement_operation_record(
                record.operation_id
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self.write_odoo_stable_target_replacement_operation_record(record)
            return True

    def recover_expired_odoo_stable_target_replacement_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        affected_operation_ids: list[str] = []
        candidates = self.list_odoo_stable_target_replacement_operation_records(
            statuses=("running",)
        )
        for candidate in candidates:
            with self._exclusive_record_lock(
                "odoo_stable_target_replacement_operations", candidate.operation_id
            ):
                record = self.read_odoo_stable_target_replacement_operation_record(
                    candidate.operation_id
                )
                if record.status != "running" or (
                    record.lease_expires_at and record.lease_expires_at >= now
                ):
                    continue
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo stable target replacement operation exhausted automatic "
                                f"attempts in safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                f"Odoo stable target replacement operation lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self.write_odoo_stable_target_replacement_operation_record(recovered_record)
        return tuple(affected_operation_ids)

    def write_odoo_prod_backup_restore_operation_record(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> Path:
        return self._write_model("odoo_prod_backup_restore_operations", record.operation_id, record)

    def read_odoo_prod_backup_restore_operation_record(
        self, operation_id: str
    ) -> OdooProdBackupRestoreOperationRecord:
        return OdooProdBackupRestoreOperationRecord.model_validate(
            self._read_model(
                OdooProdBackupRestoreOperationRecord,
                "odoo_prod_backup_restore_operations",
                operation_id,
            ).model_dump(mode="json")
        )

    def list_odoo_prod_backup_restore_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdBackupRestoreOperationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                OdooProdBackupRestoreOperationRecord,
                "odoo_prod_backup_restore_operations",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not idempotency_key or record.idempotency_key == idempotency_key)
            and (not idempotency_scope or record.idempotency_scope == idempotency_scope)
            and (not statuses or record.status in statuses)
        ]
        records.sort(key=lambda record: (record.updated_at, record.operation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def cancel_pending_odoo_prod_backup_restore_operation_record(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("Odoo backup restore cancellation requires a cancelled record.")
        with self._exclusive_record_lock(
            "odoo_prod_backup_restore_operations", record.operation_id
        ):
            current_record = self.read_odoo_prod_backup_restore_operation_record(
                record.operation_id
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self.write_odoo_prod_backup_restore_operation_record(record)
            return True

    def create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
        self, record: OdooProdBackupRestoreOperationRecord
    ) -> tuple[OdooProdBackupRestoreOperationRecord, bool]:
        reservation_id = _odoo_stable_lane_reservation_id(
            product=record.product,
            context=record.context,
            instance=record.instance,
        )
        with self._exclusive_record_lock("odoo_stable_lane_operation_reservations", reservation_id):
            active_operation = self._active_odoo_stable_lane_operation(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_operation is not None:
                if isinstance(active_operation, OdooProdBackupRestoreOperationRecord):
                    return active_operation, False
                raise OdooStableLaneOperationConflictError(
                    _odoo_stable_lane_operation_owner(active_operation)
                )
            self.write_odoo_prod_backup_restore_operation_record(record)
            return record, True

    def requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        queued_at: str,
        authorization: DurableOperationAuthorization,
    ) -> OdooProdBackupRestoreOperationRecord | None:
        reservation_id = ""
        current_record = self.read_odoo_prod_backup_restore_operation_record(operation_id.strip())
        reservation_id = _odoo_stable_lane_reservation_id(
            product=current_record.product,
            context=current_record.context,
            instance=current_record.instance,
        )
        with self._exclusive_record_lock("odoo_stable_lane_operation_reservations", reservation_id):
            active_operation = self._active_odoo_stable_lane_operation(
                product=current_record.product,
                context=current_record.context,
                instance=current_record.instance,
            )
            if active_operation is not None:
                return None
            with self._exclusive_record_lock(
                "odoo_prod_backup_restore_operations", current_record.operation_id
            ):
                locked_record = self.read_odoo_prod_backup_restore_operation_record(
                    current_record.operation_id
                )
                if locked_record.status != "fail":
                    return None
                requeued_record = requeue_odoo_prod_backup_restore_verification_replay(
                    operation=locked_record,
                    queued_at=queued_at,
                    authorization=authorization,
                )
                self.write_odoo_prod_backup_restore_operation_record(requeued_record)
                return requeued_record

    def claim_next_odoo_prod_backup_restore_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdBackupRestoreOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner or not lease_expires_at.strip() or not claimed_at.strip():
            raise ValueError("Odoo backup restore claim requires lease evidence.")
        pending_records = self.list_odoo_prod_backup_restore_operation_records(
            statuses=("pending",)
        )
        for record in sorted(
            pending_records,
            key=lambda item: (item.created_at, item.operation_id),
        ):
            reservation_id = _odoo_stable_lane_reservation_id(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            with self._exclusive_record_lock(
                "odoo_stable_lane_operation_reservations",
                reservation_id,
            ):
                active_operation = self._active_odoo_stable_lane_operation(
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_operation is None or _odoo_stable_lane_operation_owner(
                    active_operation
                ) != OdooStableLaneOperationOwner(
                    operation_kind="prod_backup_restore",
                    operation_id=record.operation_id,
                ):
                    continue
                with self._exclusive_record_lock(
                    "odoo_prod_backup_restore_operations", record.operation_id
                ):
                    current_record = self.read_odoo_prod_backup_restore_operation_record(
                        record.operation_id
                    )
                    if current_record.status != "pending":
                        continue
                    claimed_record = current_record.model_copy(
                        update={
                            "status": "running",
                            "phase": current_record.phase
                            if current_record.checkpoints
                            else "running",
                            "started_at": current_record.started_at or claimed_at,
                            "updated_at": claimed_at,
                            "lease_owner": normalized_lease_owner,
                            "lease_expires_at": lease_expires_at.strip(),
                            "heartbeat_at": claimed_at.strip(),
                            "attempt": current_record.attempt + 1,
                        }
                    )
                    self.write_odoo_prod_backup_restore_operation_record(claimed_record)
                    return claimed_record
        return None

    def heartbeat_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._exclusive_record_lock("odoo_prod_backup_restore_operations", operation_id):
            record = self.read_odoo_prod_backup_restore_operation_record(operation_id)
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self.write_odoo_prod_backup_restore_operation_record(heartbeat_record)
            return True

    def checkpoint_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdBackupRestoreOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdBackupRestoreOperationRecord | None:
        with self._exclusive_record_lock("odoo_prod_backup_restore_operations", operation_id):
            record = self.read_odoo_prod_backup_restore_operation_record(operation_id)
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= checkpointed_at.strip()
            ):
                return None
            phase_indexes = {
                phase_name: index
                for index, phase_name in enumerate(
                    ODOO_PROD_BACKUP_RESTORE_OPERATION_PHASE_SEQUENCE
                )
            }
            replay_restart = (
                phase == "post_deploy_started"
                and odoo_prod_backup_restore_operation_is_verification_replay_claim(record)
            )
            if not replay_restart and phase_indexes[phase] < phase_indexes[record.phase]:
                return None
            checkpoint = OdooProdBackupRestoreCheckpoint(
                phase=phase,
                recorded_at=checkpointed_at,
                evidence=evidence,
            )
            checkpointed_record = record.model_copy(
                update={
                    "phase": phase,
                    "checkpoints": (*record.checkpoints, checkpoint),
                    "updated_at": checkpointed_at,
                }
            )
            self.write_odoo_prod_backup_restore_operation_record(checkpointed_record)
            return checkpointed_record

    def complete_odoo_prod_backup_restore_operation_record(
        self,
        *,
        record: OdooProdBackupRestoreOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._exclusive_record_lock(
            "odoo_prod_backup_restore_operations", record.operation_id
        ):
            current_record = self.read_odoo_prod_backup_restore_operation_record(
                record.operation_id
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self.write_odoo_prod_backup_restore_operation_record(record)
            return True

    def recover_expired_odoo_prod_backup_restore_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        affected_operation_ids: list[str] = []
        candidates = self.list_odoo_prod_backup_restore_operation_records(statuses=("running",))
        for candidate in candidates:
            with self._exclusive_record_lock(
                "odoo_prod_backup_restore_operations", candidate.operation_id
            ):
                record = self.read_odoo_prod_backup_restore_operation_record(candidate.operation_id)
                if record.status != "running" or (
                    record.lease_expires_at and record.lease_expires_at >= now
                ):
                    continue
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "phase": "created",
                            "checkpoints": (),
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo production backup restore exhausted automatic attempts in "
                                f"safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "result": None,
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                "Odoo production backup restore lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self.write_odoo_prod_backup_restore_operation_record(recovered_record)
        return tuple(affected_operation_ids)

    def write_odoo_prod_retained_volume_backup_import_operation_record(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> Path:
        return self._write_model(
            "odoo_prod_retained_volume_backup_import_operations",
            record.operation_id,
            record,
        )

    def read_odoo_prod_retained_volume_backup_import_operation_record(
        self, operation_id: str
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord:
        return OdooProdRetainedVolumeBackupImportOperationRecord.model_validate(
            self._read_model(
                OdooProdRetainedVolumeBackupImportOperationRecord,
                "odoo_prod_retained_volume_backup_import_operations",
                operation_id,
            ).model_dump(mode="json")
        )

    def list_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        operation_kind: str = "",
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                OdooProdRetainedVolumeBackupImportOperationRecord,
                "odoo_prod_retained_volume_backup_import_operations",
            )
            if (not operation_kind or record.operation_kind == operation_kind)
            and (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not idempotency_key or record.idempotency_key == idempotency_key)
            and (not idempotency_scope or record.idempotency_scope == idempotency_scope)
            and (not statuses or record.status in statuses)
        ]
        records.sort(key=lambda record: (record.updated_at, record.operation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def cancel_pending_odoo_prod_retained_volume_backup_import_operation_record(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError(
                "Odoo retained-volume backup import cancellation requires a cancelled record."
            )
        with self._exclusive_record_lock(
            "odoo_prod_retained_volume_backup_import_operations",
            record.operation_id,
        ):
            current_record = self.read_odoo_prod_retained_volume_backup_import_operation_record(
                record.operation_id
            )
            if not odoo_stable_lane_cancellation_is_allowed(
                current_status=current_record.status,
                reconciliation_required_at=current_record.updated_at,
                cancellation=record.cancellation,
            ):
                return False
            self.write_odoo_prod_retained_volume_backup_import_operation_record(record)
            return True

    def create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
        self, record: OdooProdRetainedVolumeBackupImportOperationRecord
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, bool]:
        reservation_id = _odoo_stable_lane_reservation_id(
            product=record.product,
            context=record.context,
            instance=record.instance,
        )
        with self._exclusive_record_lock(
            "odoo_stable_lane_operation_reservations",
            reservation_id,
        ):
            active_operation = self._active_odoo_stable_lane_operation(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            if active_operation is not None:
                if isinstance(
                    active_operation,
                    OdooProdRetainedVolumeBackupImportOperationRecord,
                ):
                    return active_operation, False
                raise OdooStableLaneOperationConflictError(
                    _odoo_stable_lane_operation_owner(active_operation)
                )
            self.write_odoo_prod_retained_volume_backup_import_operation_record(record)
            return record, True

    def _active_odoo_stable_lane_operation(
        self,
        *,
        product: str,
        context: str,
        instance: str,
    ) -> OdooStableLaneOperationRecord | None:
        active_operations: list[OdooStableLaneOperationRecord] = []
        active_operations.extend(
            self.list_odoo_stable_bootstrap_operation_records(
                product=product,
                context_name=context,
                instance_name=instance,
                statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            )
        )
        active_operations.extend(
            self.list_odoo_stable_target_replacement_operation_records(
                product=product,
                context_name=context,
                instance_name=instance,
                statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            )
        )
        active_operations.extend(
            self.list_odoo_prod_backup_restore_operation_records(
                product=product,
                context_name=context,
                instance_name=instance,
                statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            )
        )
        active_operations.extend(
            self.list_odoo_prod_retained_volume_backup_import_operation_records(
                product=product,
                context_name=context,
                instance_name=instance,
                statuses=ODOO_STABLE_LANE_BLOCKING_STATUSES,
            )
        )
        if not active_operations:
            return None
        return min(
            active_operations,
            key=lambda operation: odoo_stable_lane_operation_priority(
                status=operation.status,
                created_at=operation.created_at,
                operation_kind=_odoo_stable_lane_operation_owner(operation).operation_kind,
                operation_id=operation.operation_id,
            ),
        )

    def claim_next_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner or not lease_expires_at.strip() or not claimed_at.strip():
            raise ValueError("Odoo retained-volume backup import claim requires lease evidence.")
        pending_records = self.list_odoo_prod_retained_volume_backup_import_operation_records(
            statuses=("pending",)
        )
        for record in sorted(
            pending_records,
            key=lambda item: (item.created_at, item.operation_id),
        ):
            reservation_id = _odoo_stable_lane_reservation_id(
                product=record.product,
                context=record.context,
                instance=record.instance,
            )
            with self._exclusive_record_lock(
                "odoo_stable_lane_operation_reservations",
                reservation_id,
            ):
                active_operation = self._active_odoo_stable_lane_operation(
                    product=record.product,
                    context=record.context,
                    instance=record.instance,
                )
                if active_operation is None or _odoo_stable_lane_operation_owner(
                    active_operation
                ) != OdooStableLaneOperationOwner(
                    operation_kind="retained_volume_backup_import",
                    operation_id=record.operation_id,
                ):
                    continue
                with self._exclusive_record_lock(
                    "odoo_prod_retained_volume_backup_import_operations",
                    record.operation_id,
                ):
                    current_record = (
                        self.read_odoo_prod_retained_volume_backup_import_operation_record(
                            record.operation_id
                        )
                    )
                    if current_record.status != "pending":
                        continue
                    claimed_record = current_record.model_copy(
                        update={
                            "status": "running",
                            "phase": "running",
                            "started_at": current_record.started_at or claimed_at,
                            "updated_at": claimed_at,
                            "lease_owner": normalized_lease_owner,
                            "lease_expires_at": lease_expires_at.strip(),
                            "heartbeat_at": claimed_at.strip(),
                            "attempt": current_record.attempt + 1,
                        }
                    )
                    self.write_odoo_prod_retained_volume_backup_import_operation_record(
                        claimed_record
                    )
                    return claimed_record
        return None

    def heartbeat_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._exclusive_record_lock(
            "odoo_prod_retained_volume_backup_import_operations",
            operation_id,
        ):
            record = self.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation_id
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= heartbeat_at.strip()
            ):
                return False
            heartbeat_record = record.model_copy(
                update={
                    "heartbeat_at": heartbeat_at.strip(),
                    "lease_expires_at": lease_expires_at.strip(),
                    "updated_at": heartbeat_at.strip(),
                }
            )
            self.write_odoo_prod_retained_volume_backup_import_operation_record(heartbeat_record)
            return True

    def checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdRetainedVolumeBackupImportOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None:
        with self._exclusive_record_lock(
            "odoo_prod_retained_volume_backup_import_operations",
            operation_id,
        ):
            record = self.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation_id
            )
            if (
                record.status != "running"
                or record.lease_owner != lease_owner.strip()
                or not record.lease_expires_at
                or record.lease_expires_at <= checkpointed_at.strip()
            ):
                return None
            phase_indexes = {
                phase_name: index
                for index, phase_name in enumerate(
                    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_PHASE_SEQUENCE
                )
            }
            if phase_indexes[phase] < phase_indexes[record.phase]:
                return None
            checkpoint = OdooProdRetainedVolumeBackupImportCheckpoint(
                phase=phase,
                recorded_at=checkpointed_at,
                evidence=evidence,
            )
            checkpointed_record = record.model_copy(
                update={
                    "phase": phase,
                    "checkpoints": (*record.checkpoints, checkpoint),
                    "updated_at": checkpointed_at,
                }
            )
            self.write_odoo_prod_retained_volume_backup_import_operation_record(checkpointed_record)
            return checkpointed_record

    def complete_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        record: OdooProdRetainedVolumeBackupImportOperationRecord,
        lease_owner: str,
    ) -> bool:
        with self._exclusive_record_lock(
            "odoo_prod_retained_volume_backup_import_operations",
            record.operation_id,
        ):
            current_record = self.read_odoo_prod_retained_volume_backup_import_operation_record(
                record.operation_id
            )
            completed_at = _utc_now_timestamp()
            if (
                current_record.status != "running"
                or current_record.lease_owner != lease_owner.strip()
                or not current_record.lease_expires_at
                or current_record.lease_expires_at <= completed_at
            ):
                return False
            self.write_odoo_prod_retained_volume_backup_import_operation_record(record)
            return True

    def recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        affected_operation_ids: list[str] = []
        candidates = self.list_odoo_prod_retained_volume_backup_import_operation_records(
            statuses=("running",)
        )
        for candidate in candidates:
            with self._exclusive_record_lock(
                "odoo_prod_retained_volume_backup_import_operations",
                candidate.operation_id,
            ):
                record = self.read_odoo_prod_retained_volume_backup_import_operation_record(
                    candidate.operation_id
                )
                if record.status != "running" or (
                    record.lease_expires_at and record.lease_expires_at >= now
                ):
                    continue
                affected_operation_ids.append(record.operation_id)
                if record.phase in safe_phases and record.attempt < max_attempts:
                    recovered_record = record.model_copy(
                        update={
                            "status": "pending",
                            "phase": "created",
                            "checkpoints": (),
                            "started_at": "",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                        }
                    )
                elif record.phase in safe_phases:
                    recovered_record = record.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": now,
                            "finished_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_attempts_exhausted",
                            "error_message": (
                                "Odoo retained-volume backup import exhausted automatic attempts "
                                f"in safe phase {record.phase!r}."
                            ),
                        }
                    )
                else:
                    recovered_record = record.model_copy(
                        update={
                            "status": "reconciliation_required",
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": "",
                            "heartbeat_at": "",
                            "error_code": "operation_reconciliation_required",
                            "error_message": (
                                "Odoo retained-volume backup import lease expired in "
                                f"phase {record.phase!r}; provider state requires operator "
                                "reconciliation before the lane can be released."
                            ),
                        }
                    )
                self.write_odoo_prod_retained_volume_backup_import_operation_record(
                    recovered_record
                )
        return tuple(affected_operation_ids)

    def write_verireel_prod_backup_gate_operation_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> Path:
        return self._write_model(
            "verireel_prod_backup_gate_operations", record.operation_id, record
        )

    def create_verireel_prod_backup_gate_operation_record_if_no_active_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> tuple[VeriReelProdBackupGateOperationRecord, bool]:
        try:
            return self.read_verireel_prod_backup_gate_operation_record(record.operation_id), False
        except FileNotFoundError:
            pass
        active_records = self.list_verireel_prod_backup_gate_operation_records(
            backup_record_id=record.backup_record_id,
            statuses=("pending", "running"),
            limit=1,
        )
        if active_records:
            return active_records[0], False
        self.write_verireel_prod_backup_gate_operation_record(record)
        return record, True

    def read_verireel_prod_backup_gate_operation_record(
        self, operation_id: str
    ) -> VeriReelProdBackupGateOperationRecord:
        return VeriReelProdBackupGateOperationRecord.model_validate(
            self._read_model(
                VeriReelProdBackupGateOperationRecord,
                "verireel_prod_backup_gate_operations",
                operation_id,
            ).model_dump(mode="json")
        )

    def list_verireel_prod_backup_gate_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        backup_record_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[VeriReelProdBackupGateOperationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                VeriReelProdBackupGateOperationRecord,
                "verireel_prod_backup_gate_operations",
            )
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not backup_record_id or record.backup_record_id == backup_record_id)
            and (not statuses or record.status in statuses)
        ]
        records.sort(key=lambda record: (record.updated_at, record.operation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def cancel_pending_verireel_prod_backup_gate_operation_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> bool:
        if record.status != "cancelled" or record.phase != "cancelled":
            raise ValueError("VeriReel backup gate cancellation requires a cancelled record.")
        with self._exclusive_record_lock(
            "verireel_prod_backup_gate_operations", record.operation_id
        ):
            current_record = self.read_verireel_prod_backup_gate_operation_record(
                record.operation_id
            )
            if current_record.status != "pending":
                return False
            backup_gate_record = build_cancelled_verireel_prod_backup_gate_record(record)
            with self._product_authority_bundle_lock():
                self._write_model_locked(
                    "verireel_prod_backup_gate_operations",
                    record.operation_id,
                    record,
                )
                self._write_model_locked(
                    "backup_gates",
                    backup_gate_record.record_id,
                    backup_gate_record,
                )
            return True

    def claim_next_verireel_prod_backup_gate_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None:
        normalized_lease_owner = lease_owner.strip()
        if not normalized_lease_owner:
            raise ValueError("VeriReel prod backup gate operation claim requires lease_owner.")
        if not lease_expires_at.strip():
            raise ValueError("VeriReel prod backup gate operation claim requires lease_expires_at.")
        if not claimed_at.strip():
            raise ValueError("VeriReel prod backup gate operation claim requires claimed_at.")
        pending_records = self.list_verireel_prod_backup_gate_operation_records(
            statuses=("pending",)
        )
        if not pending_records:
            return None
        for record in sorted(
            pending_records,
            key=lambda item: (item.created_at, item.operation_id),
        ):
            with self._exclusive_record_lock(
                "verireel_prod_backup_gate_operations", record.operation_id
            ):
                current_record = self.read_verireel_prod_backup_gate_operation_record(
                    record.operation_id
                )
                if current_record.status != "pending":
                    continue
                claimed_record = current_record.model_copy(
                    update={
                        "status": "running",
                        "phase": "running",
                        "started_at": current_record.started_at or claimed_at,
                        "updated_at": claimed_at,
                        "lease_owner": normalized_lease_owner,
                        "lease_expires_at": lease_expires_at.strip(),
                        "heartbeat_at": claimed_at.strip(),
                        "attempt": current_record.attempt + 1,
                    }
                )
                self.write_verireel_prod_backup_gate_operation_record(claimed_record)
                return claimed_record
        return None

    def heartbeat_verireel_prod_backup_gate_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool:
        record = self.read_verireel_prod_backup_gate_operation_record(operation_id)
        if (
            record.status != "running"
            or record.lease_owner != lease_owner.strip()
            or not record.lease_expires_at
            or record.lease_expires_at <= heartbeat_at.strip()
        ):
            return False
        heartbeat_record = record.model_copy(
            update={
                "heartbeat_at": heartbeat_at.strip(),
                "lease_expires_at": lease_expires_at.strip(),
                "updated_at": heartbeat_at.strip(),
            }
        )
        self.write_verireel_prod_backup_gate_operation_record(heartbeat_record)
        return True

    def mark_verireel_prod_backup_gate_operation_phase(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: str,
        updated_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None:
        record = self.read_verireel_prod_backup_gate_operation_record(operation_id)
        if (
            record.status != "running"
            or record.lease_owner != lease_owner.strip()
            or not record.lease_expires_at
            or record.lease_expires_at <= updated_at.strip()
        ):
            return None
        updated_record = record.model_copy(
            update={
                "phase": phase.strip(),
                "updated_at": updated_at.strip(),
            }
        )
        self.write_verireel_prod_backup_gate_operation_record(updated_record)
        return updated_record

    def complete_verireel_prod_backup_gate_operation_record(
        self,
        *,
        record: VeriReelProdBackupGateOperationRecord,
        lease_owner: str,
    ) -> bool:
        current_record = self.read_verireel_prod_backup_gate_operation_record(record.operation_id)
        completed_at = _utc_now_timestamp()
        if (
            current_record.status != "running"
            or current_record.lease_owner != lease_owner.strip()
            or not current_record.lease_expires_at
            or current_record.lease_expires_at <= completed_at
        ):
            return False
        self.write_verireel_prod_backup_gate_operation_record(record)
        return True

    def complete_verireel_prod_backup_gate_operation_with_backup_gate_record(
        self,
        *,
        operation_record: VeriReelProdBackupGateOperationRecord,
        backup_gate_record: BackupGateRecord,
        lease_owner: str,
    ) -> bool:
        current_record = self.read_verireel_prod_backup_gate_operation_record(
            operation_record.operation_id
        )
        completed_at = _utc_now_timestamp()
        if (
            current_record.status != "running"
            or current_record.lease_owner != lease_owner.strip()
            or not current_record.lease_expires_at
            or current_record.lease_expires_at <= completed_at
        ):
            return False
        self.write_backup_gate_record(backup_gate_record)
        self.write_verireel_prod_backup_gate_operation_record(operation_record)
        return True

    def recover_expired_verireel_prod_backup_gate_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]:
        affected_operation_ids: list[str] = []
        for record in self.list_verireel_prod_backup_gate_operation_records(statuses=("running",)):
            if record.lease_expires_at and record.lease_expires_at >= now:
                continue
            affected_operation_ids.append(record.operation_id)
            if record.phase in safe_phases and record.attempt < max_attempts:
                recovered_record = record.model_copy(
                    update={
                        "status": "pending",
                        "started_at": "",
                        "updated_at": now,
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                    }
                )
            else:
                error_message = (
                    "VeriReel prod backup gate operation lease expired in "
                    f"phase {record.phase!r}; unsafe to retry automatically."
                )
                recovered_record = record.model_copy(
                    update={
                        "status": "fail",
                        "phase": "failed",
                        "updated_at": now,
                        "finished_at": now,
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "error_message": error_message,
                    }
                )
                self.write_backup_gate_record(
                    BackupGateRecord(
                        record_id=record.backup_record_id,
                        context=record.context,
                        instance=record.instance,
                        created_at=now,
                        source="launchplane-verireel-prod-backup-gate",
                        required=True,
                        status="fail",
                        evidence={"error_message": error_message},
                    )
                )
            self.write_verireel_prod_backup_gate_operation_record(recovered_record)
        return tuple(affected_operation_ids)

    def _wait_for_odoo_stable_target_replacement_reservation_owner(
        self,
        reservation_path: Path,
        deadline: float,
    ) -> str:
        while True:
            try:
                reserved_operation_id = reservation_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return ""
            if reserved_operation_id:
                return reserved_operation_id
            if time.monotonic() >= deadline:
                return ""
            time.sleep(self.odoo_target_replacement_reservation_poll_seconds)

    def _wait_for_odoo_stable_target_replacement_reserved_operation(
        self, operation_id: str, deadline: float
    ) -> OdooStableTargetReplacementOperationRecord | None:
        while True:
            try:
                return self.read_odoo_stable_target_replacement_operation_record(operation_id)
            except (FileNotFoundError, JSONDecodeError):
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.odoo_target_replacement_reservation_poll_seconds)

    def write_backup_gate_record(self, record: BackupGateRecord) -> Path:
        return self._write_model("backup_gates", record.record_id, record)

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        return BackupGateRecord.model_validate(
            self._read_model(BackupGateRecord, "backup_gates", record_id).model_dump(mode="json")
        )

    def list_backup_gate_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[BackupGateRecord, ...]:
        records = [
            record
            for record in self._list_models(BackupGateRecord, "backup_gates")
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.created_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_promotion_record(self, record: PromotionRecord) -> Path:
        return self._write_model("promotions", record.record_id, record)

    def write_promotion_evidence_records(
        self,
        *,
        promotion_record: PromotionRecord,
        inventory: EnvironmentInventory,
    ) -> Path:
        with self._product_authority_bundle_lock():
            promotion_path = self._record_path("promotions", promotion_record.record_id)
            inventory_path = self._record_path(
                "inventory", f"{inventory.context}-{inventory.instance}"
            )
            previous_promotion = promotion_path.read_bytes() if promotion_path.exists() else None
            previous_inventory = inventory_path.read_bytes() if inventory_path.exists() else None
            try:
                self._write_model_locked(
                    "inventory",
                    f"{inventory.context}-{inventory.instance}",
                    inventory,
                )
                return self._write_model_locked(
                    "promotions",
                    promotion_record.record_id,
                    promotion_record,
                )
            except Exception:
                with suppress(Exception):
                    self._restore_record_path(promotion_path, previous_promotion)
                with suppress(Exception):
                    self._restore_record_path(inventory_path, previous_inventory)
                raise

    @staticmethod
    def _restore_record_path(record_path: Path, payload: bytes | None) -> None:
        if payload is None:
            record_path.unlink(missing_ok=True)
            return
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(payload)

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return PromotionRecord.model_validate(
            self._read_model(PromotionRecord, "promotions", record_id).model_dump(mode="json")
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        records = [
            record
            for record in self._list_models(PromotionRecord, "promotions")
            if (not context_name or record.context == context_name)
            and (not from_instance_name or record.from_instance == from_instance_name)
            and (not to_instance_name or record.to_instance == to_instance_name)
        ]
        records.sort(
            key=lambda record: (
                *self._record_sort_timestamp(record.deploy.finished_at, record.deploy.started_at),
                record.record_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_deployment_record(self, record: DeploymentRecord) -> Path:
        return self._write_model("deployments", record.record_id, record)

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        return DeploymentRecord.model_validate(
            self._read_model(DeploymentRecord, "deployments", record_id).model_dump(mode="json")
        )

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        records = [
            record
            for record in self._list_models(DeploymentRecord, "deployments")
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(
            key=lambda record: (
                *self._record_sort_timestamp(record.deploy.finished_at, record.deploy.started_at),
                record.record_id,
            ),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_generic_web_rollback_plan_record(self, record: GenericWebRollbackPlanRecord) -> Path:
        return self._write_model("generic_web_rollback_plans", record.plan_id, record)

    def list_generic_web_rollback_plan_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[GenericWebRollbackPlanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                GenericWebRollbackPlanRecord, "generic_web_rollback_plans"
            )
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.created_at, record.plan_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_environment_inventory(self, record: EnvironmentInventory) -> Path:
        return self._write_model("inventory", f"{record.context}-{record.instance}", record)

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        record_id = f"{context_name}-{instance_name}"
        return EnvironmentInventory.model_validate(
            self._read_model(EnvironmentInventory, "inventory", record_id).model_dump(mode="json")
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(EnvironmentInventory, "inventory")

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> Path:
        return self._write_model(
            "odoo_instance_overrides", f"{record.context}-{record.instance}", record
        )

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord:
        record_id = f"{context_name}-{instance_name}"
        return OdooInstanceOverrideRecord.model_validate(
            self._read_model(
                OdooInstanceOverrideRecord, "odoo_instance_overrides", record_id
            ).model_dump(mode="json")
        )

    def list_odoo_instance_override_records(self) -> tuple[OdooInstanceOverrideRecord, ...]:
        records = list(self._list_models(OdooInstanceOverrideRecord, "odoo_instance_overrides"))
        records.sort(key=lambda record: (record.context, record.instance))
        return tuple(records)

    def write_preview_record(self, record: PreviewRecord) -> Path:
        return self._write_model("launchplane_previews", record.preview_id, record)

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        return PreviewRecord.model_validate(
            self._read_model(PreviewRecord, "launchplane_previews", preview_id).model_dump(
                mode="json"
            )
        )

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        records = [
            record
            for record in self._list_models(PreviewRecord, "launchplane_previews")
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        records.sort(key=lambda record: (record.updated_at, record.preview_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_enablement_record(self, record: PreviewEnablementRecord) -> Path:
        return self._write_model("launchplane_preview_enablement", record.record_id, record)

    def read_preview_enablement_record(self, record_id: str) -> PreviewEnablementRecord:
        return PreviewEnablementRecord.model_validate(
            self._read_model(
                PreviewEnablementRecord, "launchplane_preview_enablement", record_id
            ).model_dump(mode="json")
        )

    def list_preview_enablement_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        pr_state: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewEnablementRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewEnablementRecord, "launchplane_preview_enablement"
            )
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (not pr_state or record.pr_state == pr_state)
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> Path:
        return self._write_model("launchplane_preview_generations", record.generation_id, record)

    def write_preview_generation_evidence_records(
        self,
        *,
        preview_record: PreviewRecord,
        generation_record: PreviewGenerationRecord,
    ) -> tuple[Path, Path]:
        generation_path = self._record_path(
            "launchplane_preview_generations",
            generation_record.generation_id,
        )
        preview_path = self._record_path("launchplane_previews", preview_record.preview_id)
        previous_generation = generation_path.read_bytes() if generation_path.exists() else None
        previous_preview = preview_path.read_bytes() if preview_path.exists() else None
        try:
            written_generation_path = self.write_preview_generation_record(generation_record)
            written_preview_path = self.write_preview_record(preview_record)
        except Exception:
            with suppress(Exception):
                self._restore_record_path(generation_path, previous_generation)
            with suppress(Exception):
                self._restore_record_path(preview_path, previous_preview)
            raise
        return written_generation_path, written_preview_path

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        return PreviewGenerationRecord.model_validate(
            self._read_model(
                PreviewGenerationRecord,
                "launchplane_preview_generations",
                generation_id,
            ).model_dump(mode="json")
        )

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewGenerationRecord, "launchplane_preview_generations"
            )
            if not preview_id or record.preview_id == preview_id
        ]
        records.sort(key=lambda record: (record.sequence, record.generation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> Path:
        return self._write_model("launchplane_preview_inventory_scans", record.scan_id, record)

    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewInventoryScanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewInventoryScanRecord, "launchplane_preview_inventory_scans"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.scanned_at, record.scan_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_desired_state_record(self, record: PreviewDesiredStateRecord) -> Path:
        return self._write_model(
            "launchplane_preview_desired_states", record.desired_state_id, record
        )

    def list_preview_desired_state_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewDesiredStateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewDesiredStateRecord, "launchplane_preview_desired_states"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(
            key=lambda record: (record.discovered_at, record.desired_state_id), reverse=True
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_lifecycle_plan_record(self, record: PreviewLifecyclePlanRecord) -> Path:
        return self._write_model("launchplane_preview_lifecycle_plans", record.plan_id, record)

    def list_preview_lifecycle_plan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecyclePlanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewLifecyclePlanRecord, "launchplane_preview_lifecycle_plans"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.planned_at, record.plan_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_lifecycle_cleanup_record(self, record: PreviewLifecycleCleanupRecord) -> Path:
        return self._write_model(
            "launchplane_preview_lifecycle_cleanups", record.cleanup_id, record
        )

    def list_preview_lifecycle_cleanup_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecycleCleanupRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewLifecycleCleanupRecord, "launchplane_preview_lifecycle_cleanups"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.requested_at, record.cleanup_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_pr_feedback_record(self, record: PreviewPrFeedbackRecord) -> Path:
        return self._write_model("launchplane_preview_pr_feedback", record.feedback_id, record)

    def list_preview_pr_feedback_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewPrFeedbackRecord, "launchplane_preview_pr_feedback"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.requested_at, record.feedback_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_pr_feedback_notification_policy_record(
        self, record: PreviewPrFeedbackNotificationPolicyRecord
    ) -> Path:
        return self._write_model(
            "launchplane_preview_pr_feedback_notification_policies",
            record.policy_id,
            record,
        )

    def list_preview_pr_feedback_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewPrFeedbackNotificationPolicyRecord,
                "launchplane_preview_pr_feedback_notification_policies",
            )
            if (not product or record.product in {"", product})
            and (not context_name or record.context in {"", context_name})
            and (not repository or record.repository in {"", repository})
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.policy_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_pr_feedback_notification_attempt_record(
        self, record: PreviewPrFeedbackNotificationAttemptRecord
    ) -> Path:
        return self._write_model(
            "launchplane_preview_pr_feedback_notification_attempts",
            record.attempt_id,
            record,
        )

    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewPrFeedbackNotificationAttemptRecord,
                "launchplane_preview_pr_feedback_notification_attempts",
            )
            if (not feedback_id or record.feedback_id == feedback_id)
            and (not event or record.event == event)
            and (not destination_kind or record.destination_kind == destination_kind)
        ]
        records.sort(key=lambda record: (record.attempted_at, record.attempt_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)


def _odoo_target_replacement_lane_reservation_id(
    record: OdooStableTargetReplacementOperationRecord,
) -> str:
    lane_key = "|".join((record.product, record.context, record.instance))
    digest = hashlib.sha256(lane_key.encode()).hexdigest()[:16]
    return f"{record.product}-{record.context}-{record.instance}".replace("/", "-") + f"-{digest}"


def _odoo_stable_lane_reservation_id(*, product: str, context: str, instance: str) -> str:
    lane_key = "|".join((product, context, instance))
    digest = hashlib.sha256(lane_key.encode()).hexdigest()[:16]
    return f"{product}-{context}-{instance}".replace("/", "-") + f"-{digest}"


def _odoo_stable_lane_operation_owner(
    operation: OdooStableLaneOperationRecord,
) -> OdooStableLaneOperationOwner:
    operation_kind: OdooStableLaneOperationKind
    if isinstance(operation, OdooStableBootstrapOperationRecord):
        operation_kind = "stable_bootstrap"
    elif isinstance(operation, OdooStableTargetReplacementOperationRecord):
        operation_kind = "target_replacement"
    elif isinstance(operation, OdooProdBackupRestoreOperationRecord):
        operation_kind = "prod_backup_restore"
    else:
        operation_kind = "retained_volume_backup_import"
    return OdooStableLaneOperationOwner(
        operation_kind=operation_kind,
        operation_id=operation.operation_id,
    )


def _odoo_stable_bootstrap_lane_reservation_id(
    record: OdooStableBootstrapOperationRecord,
) -> str:
    lane_key = "|".join((record.product, record.context, record.instance))
    digest = hashlib.sha256(lane_key.encode()).hexdigest()[:16]
    return f"{record.product}-{record.context}-{record.instance}".replace("/", "-") + f"-{digest}"


def _runner_host_hygiene_audit_record_id(audit_record_key: str) -> str:
    digest = hashlib.sha256(audit_record_key.encode()).hexdigest()[:16]
    normalized_key = audit_record_key.strip()
    filename_prefix = "".join(
        character if character.isascii() and (character.isalnum() or character in "._-") else "-"
        for character in normalized_key
    )[:200]
    return f"{filename_prefix or 'audit'}-{digest}"


def _runner_lane_registration_audit_record_id(audit_record_key: str) -> str:
    digest = hashlib.sha256(audit_record_key.encode()).hexdigest()[:16]
    return audit_record_key.strip().replace("/", "-") + f"-{digest}"


def _context_instance_record_id(context: str, instance: str) -> str:
    return _record_id_from_parts(context, instance)


def _runtime_environment_record_id(record: RuntimeEnvironmentRecord) -> str:
    if record.scope == "global":
        return "global"
    if record.scope == "context":
        return _record_id_from_parts(record.scope, record.context)
    return _record_id_from_parts(record.scope, record.context, record.instance)


def _record_id_from_parts(*parts: str) -> str:
    normalized_parts = tuple(part.strip() for part in parts)
    raw_id = "-".join(normalized_parts)
    digest = hashlib.sha256("\x1f".join(normalized_parts).encode("utf-8")).hexdigest()[:12]
    return raw_id.replace("/", "-").replace("\\", "-") + f"-{digest}"


def _merge_train_controller_storage_id(controller_key: str) -> str:
    return hashlib.sha256(controller_key.encode("utf-8")).hexdigest()


def _edge_endpoint_record_id(endpoint_key: str) -> str:
    return endpoint_key.replace("/", "%2F").replace("\\", "%5C")


def _private_health_endpoint_record_id(endpoint_key: str) -> str:
    return endpoint_key.replace("/", "%2F").replace("\\", "%5C")


def _route_binding_record_id(binding_key: str) -> str:
    return hashlib.sha256(binding_key.encode("utf-8")).hexdigest()


def _ingress_canary_route_record_id(canary_key: str) -> str:
    return canary_key.replace("/", "%2F").replace("\\", "%5C")
