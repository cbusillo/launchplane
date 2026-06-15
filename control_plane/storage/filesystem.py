import hashlib
import json
import os
import time
from json import JSONDecodeError
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.agent_write_intent import AgentWriteIntentRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    claim_every_code_work_request,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.generic_web_rollback import GenericWebRollbackPlanRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
)
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
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
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_health_monitoring_migration import (
    migrate_product_profile_health_monitoring_payload,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord

RecordModel = TypeVar("RecordModel", bound=BaseModel)


class FilesystemRecordStore:
    odoo_stable_bootstrap_reservation_settle_timeout_seconds = 30.0
    odoo_stable_bootstrap_reservation_poll_seconds = 0.01
    odoo_target_replacement_reservation_settle_timeout_seconds = 30.0
    odoo_target_replacement_reservation_poll_seconds = 0.01

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def _record_path(self, record_type: str, record_id: str) -> Path:
        return self.state_dir / record_type / f"{record_id}.json"

    def _write_model(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        record_path = self._record_path(record_type, record_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(model.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return record_path

    def _create_model_if_absent(self, record_type: str, record_id: str, model: BaseModel) -> bool:
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
        record_path = self._record_path(record_type, record_id)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)

    def _record_dir(self, record_type: str) -> Path:
        return self.state_dir / record_type

    def _list_models(
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

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> Path:
        return self._write_model("launchplane_authz_policies", record.record_id, record)

    def write_runtime_key_safety_policy_record(self, record: RuntimeKeySafetyPolicyRecord) -> Path:
        return self._write_model(
            "launchplane_runtime_key_safety_policies", record.record_id, record
        )

    def write_runner_host_hygiene_audit_record(
        self, record: RunnerHostHygieneApplyAuditRecord
    ) -> Path:
        return self._write_model(
            "launchplane_runner_host_hygiene_audits",
            _runner_host_hygiene_audit_record_id(record.audit_record_key),
            record,
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
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: record.audit_record_key, reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

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
        return self._write_model("launchplane_every_code_work_requests", record.request_id, record)

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
    ) -> EveryCodeWorkRequestRecord | None:
        record = self.read_every_code_work_request_record(request_id)
        claimed_record = claim_every_code_work_request(record, host=host, claimed_at=claimed_at)
        if claimed_record is None:
            return None
        self.write_every_code_work_request_record(claimed_record)
        return claimed_record

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
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_incident_record(self, record: PublicIngressIncidentRecord) -> Path:
        return self._write_model("launchplane_public_ingress_incidents", record.incident_id, record)

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

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        return self._read_product_profile_record_path(
            self._record_path("launchplane_product_profiles", product)
        )

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
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
        migrated_payload = migrate_product_profile_health_monitoring_payload(payload)
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
                if reserved_operation is not None and reserved_operation.status in {
                    "pending",
                    "running",
                }:
                    return reserved_operation, False
            reservation_path.unlink(missing_ok=True)
            return self.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(record)
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

    def create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
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
                if reserved_operation is not None and reserved_operation.status in {
                    "pending",
                    "running",
                }:
                    return reserved_operation, False
            reservation_path.unlink(missing_ok=True)
            return self.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                record
            )
        self.write_odoo_stable_target_replacement_operation_record(record)
        return record, True

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


def _odoo_stable_bootstrap_lane_reservation_id(
    record: OdooStableBootstrapOperationRecord,
) -> str:
    lane_key = "|".join((record.product, record.context, record.instance))
    digest = hashlib.sha256(lane_key.encode()).hexdigest()[:16]
    return f"{record.product}-{record.context}-{record.instance}".replace("/", "-") + f"-{digest}"


def _runner_host_hygiene_audit_record_id(audit_record_key: str) -> str:
    digest = hashlib.sha256(audit_record_key.encode()).hexdigest()[:16]
    return audit_record_key.strip().replace("/", "-") + f"-{digest}"


def _runner_lane_registration_audit_record_id(audit_record_key: str) -> str:
    digest = hashlib.sha256(audit_record_key.encode()).hexdigest()[:16]
    return audit_record_key.strip().replace("/", "-") + f"-{digest}"


def _edge_endpoint_record_id(endpoint_key: str) -> str:
    return endpoint_key.replace("/", "%2F").replace("\\", "%5C")


def _ingress_canary_route_record_id(canary_key: str) -> str:
    return canary_key.replace("/", "%2F").replace("\\", "%5C")
