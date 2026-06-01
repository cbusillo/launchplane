import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactAddonSelector,
    ArtifactAddonSource,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deploy_target import DeployedTargetReference
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackDeployPlan,
    GenericWebRollbackPlanRecord,
)
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchRecordStatus,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    MergeTrainStackCollapseRecordStatus,
    build_merge_train_stack_collapse_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.odoo_instance_override_record import OdooAddonSettingOverride
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_instance_override_record import OdooWebsiteBootstrapPayload
from control_plane.contracts.odoo_instance_override_record import OdooWebsiteBootstrapRoute
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressTargetObservation
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills


def _artifact_identity(artifact_id: str) -> ArtifactIdentityReference:
    return ArtifactIdentityReference(artifact_id=artifact_id)


def _resolved_target() -> ResolvedTargetEvidence:
    return ResolvedTargetEvidence(
        target_type="compose",
        target_id="compose-123",
        target_name="opw-prod",
    )


def _post_deploy_pass() -> PostDeployUpdateEvidence:
    return PostDeployUpdateEvidence(
        attempted=True,
        status="pass",
        detail="Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
    )


def _health_pass() -> HealthcheckEvidence:
    return HealthcheckEvidence(
        verified=True,
        urls=("https://prod.example.com/web/health",),
        timeout_seconds=45,
        status="pass",
    )


def _merge_train_batch_candidate_record(
    *,
    record_id: str = "merge-train-batch-candidate-20260514T010000Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:00:00Z",
) -> MergeTrainBatchCandidateRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    base_sha = "base-main"
    entries = (
        MergeTrainBatchEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            title="First queued change",
            url="https://github.com/example/merge-train-repo/pull/10",
        ),
        MergeTrainBatchEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            title="Second queued change",
            url="https://github.com/example/merge-train-repo/pull/11",
        ),
    )
    batch_id = build_merge_train_batch_id(
        repository=repository,
        base_branch=base_branch,
        base_sha=base_sha,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidateRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        candidate=MergeTrainBatchCandidate(
            batch_id=batch_id,
            repository=repository,
            base_branch=base_branch,
            base_sha=base_sha,
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            candidate_ref=build_merge_train_batch_candidate_ref(
                repository=repository,
                base_branch=base_branch,
                batch_id=batch_id,
            ),
            candidate_sha="candidate-sha",
            status="passed",
            entries=entries,
            required_checks_status="pass",
            created_at="2026-05-14T00:59:00Z",
            updated_at=updated_at,
        ),
    )


def _runner_host_hygiene_audit_record(
    *,
    audit_record_key: str,
    status: RunnerHostHygieneApplyAuditStatus = "planned",
    message: str = "planned runner host hygiene apply; no host mutation was executed",
) -> RunnerHostHygieneApplyAuditRecord:
    report = evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
        ),
    )
    request = RunnerHostHygieneApplyRequest(
        action="prune_docker_cache",
        host_name="chris-testing",
        mutate=True,
        retained_warm_builders=("odoo-docker-chris-testing",),
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=("chris-testing",),
            required_retained_warm_builders=("odoo-docker-chris-testing",),
            allow_docker_cache_prune=True,
        ),
        request=request,
        report=report,
    )
    return RunnerHostHygieneApplyAuditRecord(
        audit_record_key=audit_record_key,
        status=status,
        request=request,
        plan=plan,
        pre_apply_report=report,
        post_apply_report=report if status != "planned" else None,
        message=message,
    )


def _merge_train_batch_landing_plan_record(
    *,
    record_id: str = "merge-train-batch-landing-plan-20260514T010500Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:05:00Z",
) -> MergeTrainBatchLandingPlanRecord:
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=_merge_train_batch_candidate_record(updated_at=updated_at).candidate,
        merge_method="squash",
        created_at=updated_at,
    )
    return MergeTrainBatchLandingPlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        landing_plan=landing_plan,
    )


def _merge_train_stack_collapse_plan_record(
    *,
    record_id: str = "merge-train-stack-collapse-plan-20260514T013000Z-active",
    status: MergeTrainStackCollapseRecordStatus = "active",
    updated_at: str = "2026-05-14T01:30:00Z",
) -> MergeTrainStackCollapsePlanRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    entries = (
        MergeTrainStackCollapseEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            head_ref="feature/root",
            base_sha="base-10",
            base_ref="main",
        ),
        MergeTrainStackCollapseEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            head_ref="feature/child",
            base_sha="base-11",
            base_ref="feature/root",
        ),
    )
    collapse_id = build_merge_train_stack_collapse_id(
        repository=repository,
        base_branch=base_branch,
        root_pull_request_number=10,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainStackCollapsePlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        plan=MergeTrainStackCollapsePlan(
            collapse_id=collapse_id,
            repository=repository,
            base_branch=base_branch,
            root_pull_request_number=10,
            root_initial_head_sha="head-10",
            root_head_ref="feature/root",
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            entries=entries,
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=11,
                    parent_pull_request_number=10,
                    child_head_sha="head-11",
                    expected_parent_head_sha="head-10",
                    parent_head_ref="feature/root",
                ),
            ),
            created_at="2026-05-14T01:29:00Z",
            updated_at=updated_at,
        ),
    )


class FilesystemRecordStoreTests(unittest.TestCase):
    def test_write_and_list_runner_host_hygiene_audit_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing"
            )
            newer_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing"
            )
            failed_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-25/chris-testing",
                status="failed",
                message="post-apply evidence reported low disk",
            )

            written_path = store.write_runner_host_hygiene_audit_record(older_record)
            store.write_runner_host_hygiene_audit_record(newer_record)
            store.write_runner_host_hygiene_audit_record(failed_record)
            planned_records = store.list_runner_host_hygiene_audit_records(
                host_name="Chris-Testing",
                status="planned",
            )
            limited_records = store.list_runner_host_hygiene_audit_records(limit=1)

        self.assertEqual(
            written_path.parent.relative_to(state_dir).as_posix(),
            "launchplane_runner_host_hygiene_audits",
        )
        self.assertTrue(written_path.name.startswith("runner-host-hygiene-2026-05-23-"))
        self.assertEqual(
            [record.audit_record_key for record in planned_records],
            [newer_record.audit_record_key, older_record.audit_record_key],
        )
        self.assertEqual(
            [record.audit_record_key for record in limited_records],
            [failed_record.audit_record_key],
        )

    def test_write_and_list_merge_train_pr_feedback_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = MergeTrainPrFeedbackRecord(
                feedback_id="merge-train-pr-feedback-example-repo-main-pr-7-old",
                repository="example/repo",
                base_branch="main",
                pull_request_number=7,
                pull_request_url="https://github.com/example/repo/pull/7",
                event="queued",
                marker="<!-- launchplane-merge-train:abc -->",
                comment_markdown="<!-- launchplane-merge-train:abc -->\nQueued.",
                source="test",
                recorded_at="2026-05-20T12:00:00Z",
                delivery_status="delivered",
                delivery_action="created_comment",
                comment_id=100,
                comment_url="https://github.com/example/repo/pull/7#issuecomment-100",
            )
            newer_record = older_record.model_copy(
                update={
                    "feedback_id": "merge-train-pr-feedback-example-repo-main-pr-7-new",
                    "event": "waiting",
                    "recorded_at": "2026-05-20T12:05:00Z",
                    "delivery_action": "updated_comment",
                    "comment_id": 101,
                    "comment_url": "https://github.com/example/repo/pull/7#issuecomment-101",
                }
            )
            other_record = older_record.model_copy(
                update={
                    "feedback_id": "merge-train-pr-feedback-example-repo-main-pr-8",
                    "pull_request_number": 8,
                    "pull_request_url": "https://github.com/example/repo/pull/8",
                    "recorded_at": "2026-05-20T12:10:00Z",
                }
            )

            written_path = store.write_merge_train_pr_feedback_record(older_record)
            store.write_merge_train_pr_feedback_record(newer_record)
            store.write_merge_train_pr_feedback_record(other_record)
            listed_records = store.list_merge_train_pr_feedback_records(
                repository="example/repo",
                base_branch="main",
                pr_number=7,
            )
            limited_records = store.list_merge_train_pr_feedback_records(
                repository="example/repo",
                base_branch="main",
                limit=1,
            )

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_merge_train_pr_feedback/"
            "merge-train-pr-feedback-example-repo-main-pr-7-old.json",
        )
        self.assertEqual(
            [record.feedback_id for record in listed_records],
            [newer_record.feedback_id, older_record.feedback_id],
        )
        self.assertEqual(
            [record.feedback_id for record in limited_records], [other_record.feedback_id]
        )

    def test_write_and_list_every_code_preview_gate_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = EveryCodePreviewGateRecord(
                gate_id="every-code-preview-gate-cbusillo-code-26-oldsha",
                request_id="every-code-cbusillo-code-123-test",
                repository="cbusillo/code",
                issue_number=123,
                issue_url="https://github.com/cbusillo/code/issues/123",
                issue_author="octocat",
                pr_number=26,
                pr_url="https://github.com/cbusillo/code/pull/26",
                head_sha="oldsha",
                status="pending",
                created_at="2026-05-06T17:00:00Z",
                updated_at="2026-05-06T17:00:00Z",
            )
            newer_record = older_record.model_copy(
                update={
                    "gate_id": "every-code-preview-gate-cbusillo-code-26-newsha",
                    "head_sha": "newsha",
                    "status": "blocked",
                    "updated_at": "2026-05-06T18:00:00Z",
                    "blocked_at": "2026-05-06T18:00:00Z",
                }
            )

            written_path = store.write_every_code_preview_gate_record(older_record)
            store.write_every_code_preview_gate_record(newer_record)
            listed_records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                pr_number=26,
            )
            blocked_records = store.list_every_code_preview_gate_records(status="blocked")
            self.assertTrue(written_path.exists())

        self.assertEqual(
            [record.gate_id for record in listed_records],
            [newer_record.gate_id, older_record.gate_id],
        )
        self.assertEqual([record.gate_id for record in blocked_records], [newer_record.gate_id])

    def test_write_and_list_every_code_pr_feedback_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = EveryCodePrFeedbackRecord(
                feedback_id="every-code-pr-feedback-cbusillo-code-26-old",
                request_id="every-code-cbusillo-code-123-test",
                repository="cbusillo/code",
                pr_number=26,
                pr_url="https://github.com/cbusillo/code/pull/26",
                feedback_kind="issue_comment",
                github_delivery_id="delivery-old",
                github_id="100",
                actor="cbusillo",
                body="Please adjust the README wording.",
                received_at="2026-05-06T17:00:00Z",
            )
            newer_record = older_record.model_copy(
                update={
                    "feedback_id": "every-code-pr-feedback-cbusillo-code-26-new",
                    "github_delivery_id": "delivery-new",
                    "github_id": "101",
                    "received_at": "2026-05-06T18:00:00Z",
                }
            )

            written_path = store.write_every_code_pr_feedback_record(older_record)
            store.write_every_code_pr_feedback_record(newer_record)
            listed_records = store.list_every_code_pr_feedback_records(
                request_id="every-code-cbusillo-code-123-test",
                status="pending",
            )
            offset_records = store.list_every_code_pr_feedback_records(
                request_id="every-code-cbusillo-code-123-test",
                status="pending",
                limit=1,
                offset=1,
            )
            self.assertTrue(written_path.exists())

        self.assertEqual(
            [record.feedback_id for record in listed_records],
            [newer_record.feedback_id, older_record.feedback_id],
        )
        self.assertEqual(
            [record.feedback_id for record in offset_records], [older_record.feedback_id]
        )

    def test_write_and_read_product_profile_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = LaunchplaneProductProfileRecord(
                product="sellyouroutboard",
                display_name="SellYourOutboard.com",
                repository="cbusillo/sellyouroutboard",
                driver_id="generic-web",
                image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
                runtime_port=3000,
                health_path="/api/health",
                lanes=(ProductLaneProfile(instance="testing", context="sellyouroutboard"),),
                preview=ProductPreviewProfile(
                    enabled=True,
                    context="sellyouroutboard-testing",
                    slug_template="pr-{number}",
                ),
                updated_at="2026-04-30T20:00:00Z",
                source="operator:test",
            )

            written_path = store.write_product_profile_record(record)
            loaded_record = store.read_product_profile_record("sellyouroutboard")
            listed_records = store.list_product_profile_records(driver_id="generic-web")
            self.assertTrue(written_path.exists())

        self.assertEqual(loaded_record.driver_id, "generic-web")
        self.assertEqual(loaded_record.preview.context, "sellyouroutboard-testing")
        self.assertEqual(
            [listed_record.product for listed_record in listed_records], ["sellyouroutboard"]
        )

    def test_write_and_list_public_ingress_observation_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = PublicIngressObservationRecord(
                record_id="public-ingress-example-site-prod-older",
                product="example-site",
                context="example-site-prod",
                instance="prod",
                observed_at="2026-05-29T12:00:00Z",
                status="pass",
                base_url="https://example.test",
                targets=(
                    PublicIngressTargetObservation(
                        target="base_url",
                        url="https://example.test",
                        status="pass",
                        http_status=200,
                        summary="Public ingress returned a successful response.",
                    ),
                ),
                summary="Public ingress is reachable.",
            )
            newer_record = older_record.model_copy(
                update={
                    "record_id": "public-ingress-example-site-prod-newer",
                    "observed_at": "2026-05-29T12:05:00Z",
                    "status": "fail",
                    "failure_code": "http_error",
                    "targets": (
                        PublicIngressTargetObservation(
                            target="base_url",
                            url="https://example.test",
                            status="fail",
                            failure_code="http_error",
                            http_status=503,
                            summary="HTTP 503",
                        ),
                    ),
                    "summary": "Public ingress failed.",
                }
            )

            written_path = store.write_public_ingress_observation_record(older_record)
            store.write_public_ingress_observation_record(newer_record)
            listed_records = store.list_public_ingress_observation_records(
                product="example-site",
                context_name="example-site-prod",
                instance_name="prod",
            )
            limited_records = store.list_public_ingress_observation_records(
                product="example-site",
                limit=1,
            )
            self.assertTrue(written_path.exists())

        self.assertEqual(
            [record.record_id for record in listed_records],
            [newer_record.record_id, older_record.record_id],
        )
        self.assertEqual([record.record_id for record in limited_records], [newer_record.record_id])

    def test_write_and_list_ingress_route_audit_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = IngressRouteAuditRecord(
                record_id="ingress-route-audit-old",
                product="launchplane",
                context="reon-prod",
                mode="dry-run",
                status="planned",
                dry_run=True,
                requested_domains=("ingress-canary.example.test",),
                expected_host_id=78,
                provider_host_id=78,
                operations=(
                    IngressRouteAuditOperation(
                        action="no-op",
                        host_id=78,
                        domain_names=("ingress-canary.example.test",),
                        requires_apply=False,
                    ),
                ),
                trace_id="launchplane_req_old",
                reason="Plan canary route.",
                recorded_at="2026-05-31T12:00:00Z",
            )
            newer_record = older_record.model_copy(
                update={
                    "record_id": "ingress-route-audit-new",
                    "mode": "apply",
                    "status": "unchanged",
                    "dry_run": False,
                    "trace_id": "launchplane_req_new",
                    "idempotency_key": "ingress-canary-apply",
                    "recorded_at": "2026-05-31T12:05:00Z",
                }
            )

            written_path = store.write_ingress_route_audit_record(older_record)
            store.write_ingress_route_audit_record(newer_record)
            listed_records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )
            limited_records = store.list_ingress_route_audit_records(product="launchplane", limit=1)
            loaded_record = store.read_ingress_route_audit_record(newer_record.record_id)
            self.assertTrue(written_path.exists())

        self.assertEqual(
            [record.record_id for record in listed_records],
            [newer_record.record_id, older_record.record_id],
        )
        self.assertEqual([record.record_id for record in limited_records], [newer_record.record_id])
        self.assertEqual(loaded_record.record_id, newer_record.record_id)

    def test_ingress_route_audit_operation_accepts_legacy_missing_categories(
        self,
    ) -> None:
        operation = IngressRouteAuditOperation.model_validate(
            {
                "action": "update",
                "host_id": 78,
                "domain_names": ["ingress-canary.example.test"],
                "requires_apply": True,
            }
        )

        self.assertEqual(operation.change_categories, ())

    def test_postgres_store_round_trips_ingress_route_audit_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = IngressRouteAuditRecord(
                record_id="ingress-route-audit-postgres",
                product="launchplane",
                context="reon-prod",
                mode="apply",
                status="unchanged",
                dry_run=False,
                requested_domains=("ingress-canary.example.test",),
                expected_host_id=78,
                provider_host_id=78,
                operations=(
                    IngressRouteAuditOperation(
                        action="no-op",
                        host_id=78,
                        domain_names=("ingress-canary.example.test",),
                        requires_apply=False,
                    ),
                ),
                trace_id="launchplane_req_postgres",
                idempotency_key="ingress-canary-apply",
                reason="Apply unchanged canary route.",
                recorded_at="2026-05-31T12:05:00Z",
            )

            store.write_ingress_route_audit_record(record)

            listed_records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )
            loaded_record = store.read_ingress_route_audit_record(record.record_id)

        self.assertEqual([stored.record_id for stored in listed_records], [record.record_id])
        self.assertEqual(listed_records[0].provider_host_id, 78)
        self.assertEqual(loaded_record.record_id, record.record_id)

    def test_write_and_list_public_ingress_incident_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            open_record = PublicIngressIncidentRecord(
                incident_id="public-ingress-incident-example-site-prod-open",
                product="example-site",
                context="example-site-prod",
                instance="prod",
                status="open",
                opened_at="2026-05-29T12:20:00Z",
                opened_observation_id="obs-1",
                latest_observation_id="obs-1",
                latest_observed_at="2026-05-29T12:20:00Z",
                failure_code="http_error",
                summary="Public ingress failed.",
            )
            resolved_record = open_record.model_copy(
                update={
                    "incident_id": "public-ingress-incident-example-site-prod-resolved",
                    "status": "resolved",
                    "latest_observation_id": "obs-2",
                    "latest_observed_at": "2026-05-29T12:25:00Z",
                    "resolved_at": "2026-05-29T12:25:00Z",
                    "resolved_observation_id": "obs-2",
                    "summary": "Public ingress recovered.",
                }
            )

            written_path = store.write_public_ingress_incident_record(open_record)
            store.write_public_ingress_incident_record(resolved_record)
            listed_records = store.list_public_ingress_incident_records(
                product="example-site",
                context_name="example-site-prod",
                instance_name="prod",
            )
            open_records = store.list_public_ingress_incident_records(status="open")

            self.assertTrue(written_path.exists())
            self.assertEqual(
                [record.incident_id for record in listed_records],
                [resolved_record.incident_id, open_record.incident_id],
            )
            self.assertEqual(
                [record.incident_id for record in open_records], [open_record.incident_id]
            )

    def test_write_and_list_public_ingress_notification_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            policy = PublicIngressNotificationPolicyRecord(
                policy_id="public-ingress-notification-policy-example-site",
                product="example-site",
                context="example-site-prod",
                instance="prod",
                status="enabled",
                destinations=(
                    PublicIngressNotificationDestination(
                        destination_id="discord-ops",
                        kind="discord",
                        discord_webhook_secret="discord-webhook",
                    ),
                ),
                created_at="2026-05-29T12:00:00Z",
                updated_at="2026-05-29T12:00:00Z",
                source="test",
            )
            attempt = PublicIngressNotificationAttemptRecord(
                attempt_id="public-ingress-notification-attempt-1",
                incident_id="incident-1",
                event="opened",
                policy_id=policy.policy_id,
                destination_id="discord-ops",
                destination_kind="discord",
                delivery_status="delivered",
                attempted_at="2026-05-29T12:20:00Z",
                observation_id="obs-1",
                action="posted_discord",
            )

            policy_path = store.write_public_ingress_notification_policy_record(policy)
            attempt_path = store.write_public_ingress_notification_attempt_record(attempt)
            policies = store.list_public_ingress_notification_policy_records(
                product="example-site",
                context_name="example-site-prod",
                status="enabled",
            )
            attempts = store.list_public_ingress_notification_attempt_records(
                incident_id="incident-1",
                destination_kind="discord",
            )

            self.assertTrue(policy_path.exists())
            self.assertTrue(attempt_path.exists())
            self.assertEqual([record.policy_id for record in policies], [policy.policy_id])
            self.assertEqual([record.attempt_id for record in attempts], [attempt.attempt_id])

    def test_write_and_list_runtime_key_safety_policy_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-20260505T190000Z-old",
                status="superseded",
                source="test",
                updated_at="2026-05-05T19:00:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SHOPIFY_ACCESS_TOKEN",
                        secret_class="testing",
                    ),
                ),
            )
            active_record = RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-20260505T200000Z-active",
                status="active",
                source="test",
                updated_at="2026-05-05T20:00:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SHOPIFY_ACCESS_TOKEN",
                        secret_class="testing",
                        allowed_contexts=("opw",),
                        allowed_instances=("testing",),
                    ),
                ),
            )

            store.write_runtime_key_safety_policy_record(older_record)
            written_path = store.write_runtime_key_safety_policy_record(active_record)
            listed_records = store.list_runtime_key_safety_policy_records(status="active")

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_runtime_key_safety_policies/"
            "runtime-key-safety-policy-20260505T200000Z-active.json",
        )
        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].rules[0].allowed_contexts, ("opw",))

    def test_write_and_list_merge_train_policy_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            policy = build_test_merge_train_policy_with_codex_skills()
            older_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T200000Z-old",
                status="superseded",
                source="test",
                updated_at="2026-05-13T20:00:00Z",
                policy=policy,
            )
            active_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T210000Z-active",
                status="active",
                source="test",
                updated_at="2026-05-13T21:00:00Z",
                policy=policy,
            )

            store.write_merge_train_policy_record(older_record)
            written_path = store.write_merge_train_policy_record(active_record)
            listed_records = store.list_merge_train_policy_records(status="active")

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_merge_train_policies/merge-train-policy-20260513T210000Z-active.json",
        )
        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(
            listed_records[0]
            .policy.find_repository_policy(repository="cbusillo/codex-skills", base_branch="main")
            .enqueue_label,
            "ready-to-merge",
        )

    def test_write_and_list_merge_train_batch_candidate_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = _merge_train_batch_candidate_record(
                record_id="merge-train-batch-candidate-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_candidate_record()

            store.write_merge_train_batch_candidate_record(older_record)
            written_path = store.write_merge_train_batch_candidate_record(active_record)
            listed_records = store.list_merge_train_batch_candidate_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_merge_train_batch_candidates/"
            "merge-train-batch-candidate-20260514T010000Z-active.json",
        )
        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].candidate.entries[1].pull_request_number, 11)

    def test_write_and_list_merge_train_batch_landing_plan_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = _merge_train_batch_landing_plan_record(
                record_id="merge-train-batch-landing-plan-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_landing_plan_record()

            store.write_merge_train_batch_landing_plan_record(older_record)
            written_path = store.write_merge_train_batch_landing_plan_record(active_record)
            listed_records = store.list_merge_train_batch_landing_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_merge_train_batch_landing_plans/"
            "merge-train-batch-landing-plan-20260514T010500Z-active.json",
        )
        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].landing_plan.entries[0].merge_method, "squash")

    def test_write_and_list_merge_train_stack_collapse_plan_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = _merge_train_stack_collapse_plan_record(
                record_id="merge-train-stack-collapse-plan-20260514T012900Z-old",
                status="superseded",
                updated_at="2026-05-14T01:29:00Z",
            )
            active_record = _merge_train_stack_collapse_plan_record()

            store.write_merge_train_stack_collapse_plan_record(older_record)
            written_path = store.write_merge_train_stack_collapse_plan_record(active_record)
            listed_records = store.list_merge_train_stack_collapse_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )

        self.assertEqual(
            written_path.relative_to(state_dir).as_posix(),
            "launchplane_merge_train_stack_collapse_plans/"
            "merge-train-stack-collapse-plan-20260514T013000Z-active.json",
        )
        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].plan.intent_source, "root_ready_to_merge")

    def test_write_and_read_artifact_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            manifest = ArtifactIdentityManifest(
                artifact_id="artifact-20260410-f45db648",
                source_commit="f45db648",
                enterprise_base_digest="sha256:enterprise123",
                addon_selectors=(
                    ArtifactAddonSelector(
                        repository="cbusillo/disable_odoo_online",
                        selector="main",
                        resolved_ref="f45db648",
                    ),
                ),
                image=ArtifactImageReference(
                    repository="ghcr.io/cbusillo/odoo-private",
                    digest="sha256:image456",
                    tags=("sha-f45db648",),
                ),
            )

            written_path = store.write_artifact_manifest(manifest)
            loaded_manifest = store.read_artifact_manifest(manifest.artifact_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_manifest.artifact_id, manifest.artifact_id)
            self.assertEqual(loaded_manifest.image.digest, "sha256:image456")
            self.assertEqual(loaded_manifest.addon_selectors[0].selector, "main")

    def test_write_and_read_release_tuple_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = ReleaseTupleRecord(
                tuple_id="opw-testing-artifact-sha256-image456",
                context="opw",
                channel="testing",
                artifact_id="artifact-sha256-image456",
                repo_shas={
                    "tenant-opw": "abc1234",
                    "shared-addons": "def5678",
                },
                image_repository="ghcr.io/cbusillo/odoo-private",
                image_digest="sha256:image456",
                deployment_record_id="deployment-1",
                provenance="ship",
                minted_at="2026-04-10T18:24:00Z",
            )

            written_path = store.write_release_tuple_record(record)
            loaded_record = store.read_release_tuple_record(
                context_name="opw",
                channel_name="testing",
            )

            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.tuple_id, record.tuple_id)
            self.assertEqual(loaded_record.repo_shas["shared-addons"], "def5678")

    def test_release_tuples_export_catalog_renders_state_records(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_release_tuple_record(
                ReleaseTupleRecord(
                    tuple_id="opw-testing-artifact-sha256-image456",
                    context="opw",
                    channel="testing",
                    artifact_id="artifact-sha256-image456",
                    repo_shas={"tenant-opw": "abc1234"},
                    deployment_record_id="deployment-1",
                    provenance="ship",
                    minted_at="2026-04-10T18:24:00Z",
                )
            )

            result = runner.invoke(
                main,
                [
                    "release-tuples",
                    "export-catalog",
                    "--state-dir",
                    str(state_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("[contexts.opw.channels.testing]", result.output)
            self.assertIn('tuple_id = "opw-testing-artifact-sha256-image456"', result.output)
            self.assertIn('tenant-opw = "abc1234"', result.output)

    def test_write_and_read_backup_gate_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = BackupGateRecord(
                record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                instance="prod",
                created_at="2026-04-10T18:22:31Z",
                source="prod-gate",
                status="pass",
                evidence={
                    "snapshot": "opw-predeploy-20260410-182231",
                    "storage": "pbs",
                },
            )

            written_path = store.write_backup_gate_record(record)
            loaded_record = store.read_backup_gate_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.instance, "prod")
            self.assertEqual(loaded_record.evidence["snapshot"], "opw-predeploy-20260410-182231")

    def test_list_backup_gate_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-10T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-1"},
                )
            )
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260411T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-11T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-2"},
                )
            )
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-staging-20260412T182231Z",
                    context="opw",
                    instance="staging",
                    created_at="2026-04-12T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-3"},
                )
            )

            listed_records = store.list_backup_gate_records(
                context_name="opw", instance_name="prod", limit=2
            )

            self.assertEqual(
                [record.record_id for record in listed_records],
                [
                    "backup-opw-prod-20260411T182231Z",
                    "backup-opw-prod-20260410T182231Z",
                ],
            )

    def test_write_and_read_promotion_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = PromotionRecord(
                record_id="promotion-20260410-182231-opw-testing-prod",
                artifact_identity=_artifact_identity("artifact-20260410-f45db648"),
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ),
            )

            written_path = store.write_promotion_record(record)
            loaded_record = store.read_promotion_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.deploy.target_name, "opw-prod")

    def test_list_promotion_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260410T182231Z-opw-testing-to-prod",
                    artifact_identity=_artifact_identity("artifact-1"),
                    backup_record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-10T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                    artifact_identity=_artifact_identity("artifact-2"),
                    backup_record_id="backup-opw-prod-20260411T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-11T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260412T182231Z-opw-staging-to-prod",
                    artifact_identity=_artifact_identity("artifact-3"),
                    backup_record_id="backup-opw-prod-20260412T182231Z",
                    context="opw",
                    from_instance="staging",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-12T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260413T182231Z-opw-testing-to-prod",
                    artifact_identity=_artifact_identity("artifact-4"),
                    backup_record_id="backup-opw-prod-20260413T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        started_at="2026-04-13T18:22:31Z",
                    ),
                )
            )

            listed_records = store.list_promotion_records(
                context_name="opw",
                from_instance_name="testing",
                to_instance_name="prod",
                limit=2,
            )

            self.assertEqual(
                [record.record_id for record in listed_records],
                [
                    "promotion-20260413T182231Z-opw-testing-to-prod",
                    "promotion-20260411T182231Z-opw-testing-to-prod",
                ],
            )

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = DeploymentRecord(
                record_id="deployment-20260410T182231Z-opw-prod",
                artifact_identity=_artifact_identity("artifact-20260410-f45db648"),
                context="opw",
                instance="prod",
                source_git_ref="abc123",
                resolved_target=_resolved_target(),
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="delegated-compose-ship",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
                post_deploy_update=_post_deploy_pass(),
                destination_health=_health_pass(),
            )

            written_path = store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.deploy.deployment_id, "delegated-compose-ship")
            self.assertEqual(loaded_record.post_deploy_update.status, "pass")
            self.assertEqual(loaded_record.destination_health.status, "pass")
            resolved_target = loaded_record.resolved_target
            assert resolved_target is not None
            self.assertEqual(resolved_target.target_id, "compose-123")
            deployed_target = loaded_record.deployed_target
            assert deployed_target is not None
            self.assertEqual(deployed_target.provider_id, "dokploy")
            self.assertEqual(deployed_target.target_category, "compose")
            self.assertEqual(deployed_target.target_id, "compose-123")
            self.assertEqual(deployed_target.display_name, "opw-prod")

    def test_deployment_record_accepts_provider_neutral_target_without_dokploy_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = DeploymentRecord(
                record_id="deployment-20260410T182231Z-syo-prod",
                artifact_identity=_artifact_identity("artifact-20260410-f45db648"),
                context="syo",
                instance="prod",
                source_git_ref="abc123",
                deployed_target=DeployedTargetReference(
                    provider_id="fake-cloud",
                    target_category="service",
                    target_id="svc-123",
                    display_name="syo-prod-service",
                    provider_target_type="managed-service",
                    provider_evidence={"region": "us-east-1"},
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-service",
                    target_type="application",
                    deploy_mode="fake-cloud-service-api",
                    provider_id="fake-cloud",
                    target_category="service",
                    provider_target_type="managed-service",
                    provider_deploy_mode="service-api",
                    deployment_id="deploy-123",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
            )

            store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)

        self.assertIsNone(loaded_record.resolved_target)
        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.target_category, "service")
        self.assertEqual(deployed_target.provider_target_type, "managed-service")
        self.assertEqual(deployed_target.provider_evidence, {"region": "us-east-1"})
        self.assertEqual(loaded_record.deploy.provider_id, "fake-cloud")
        self.assertEqual(loaded_record.deploy.target_category, "service")
        self.assertEqual(loaded_record.deploy.provider_target_type, "managed-service")

    def test_deployment_record_derives_deployed_target_provider_from_deploy_evidence(
        self,
    ) -> None:
        record = DeploymentRecord(
            record_id="deployment-20260410T182231Z-syo-prod",
            artifact_identity=_artifact_identity("artifact-20260410-f45db648"),
            context="syo",
            instance="prod",
            source_git_ref="abc123",
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="svc-123",
                target_name="syo-prod-service",
            ),
            deploy=DeploymentEvidence(
                target_name="syo-prod-service",
                target_type="application",
                deploy_mode="fake-cloud-application-api",
                provider_id="fake-cloud",
                target_category="service",
                provider_target_type="managed-service",
                provider_deploy_mode="application-api",
                deployment_id="deploy-123",
                status="pass",
                started_at="2026-04-10T18:22:31Z",
                finished_at="2026-04-10T18:24:00Z",
            ),
        )

        deployed_target = record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.provider_target_type, "managed-service")
        self.assertEqual(deployed_target.target_id, "svc-123")

    def test_list_deployment_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260410T182231Z-opw-prod",
                    artifact_identity=_artifact_identity("artifact-1"),
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-10T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260411T182231Z-opw-prod",
                    artifact_identity=_artifact_identity("artifact-2"),
                    context="opw",
                    instance="prod",
                    source_git_ref="def456",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-11T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260412T182231Z-opw-staging",
                    artifact_identity=_artifact_identity("artifact-3"),
                    context="opw",
                    instance="staging",
                    source_git_ref="ghi789",
                    deploy=DeploymentEvidence(
                        target_name="opw-staging",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-12T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260413T182231Z-opw-prod",
                    artifact_identity=_artifact_identity("artifact-4"),
                    context="opw",
                    instance="prod",
                    source_git_ref="jkl012",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        started_at="2026-04-13T18:22:31Z",
                    ),
                )
            )

            listed_records = store.list_deployment_records(
                context_name="opw", instance_name="prod", limit=2
            )

            self.assertEqual(
                [record.record_id for record in listed_records],
                [
                    "deployment-20260413T182231Z-opw-prod",
                    "deployment-20260411T182231Z-opw-prod",
                ],
            )

    def test_write_and_list_generic_web_rollback_plan_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            older_record = GenericWebRollbackPlanRecord(
                plan_id="rollback-plan-older",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                status="ready",
                rollback_deployment_record_id="deployment-syo-prod-older",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:older"
                ),
                planned_deploy=GenericWebRollbackDeployPlan(
                    product="sellyouroutboard",
                    instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:older",
                    source_git_ref="older",
                ),
                source_git_ref="older",
                backup_gate=BackupGateEvidence(required=False, status="skipped"),
                target_health=HealthcheckEvidence(status="pass"),
                created_at="2026-05-01T21:00:00Z",
                summary="generic web rollback plan is ready",
            )
            newer_record = older_record.model_copy(
                update={
                    "plan_id": "rollback-plan-newer",
                    "rollback_deployment_record_id": "deployment-syo-prod-newer",
                    "created_at": "2026-05-01T22:00:00Z",
                }
            )

            written_path = store.write_generic_web_rollback_plan_record(older_record)
            store.write_generic_web_rollback_plan_record(newer_record)

            self.assertTrue(written_path.exists())
            listed_records = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing", instance_name="prod"
            )
            self.assertEqual(
                [record.plan_id for record in listed_records],
                ["rollback-plan-newer", "rollback-plan-older"],
            )

    def test_artifacts_ingest_writes_manifest(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "artifact-manifest.json"
            input_file.write_text(
                ArtifactIdentityManifest(
                    artifact_id="artifact-sha256-image456",
                    source_commit="f45db648",
                    enterprise_base_digest="sha256:enterprise123",
                    addon_sources=(
                        ArtifactAddonSource(repository="cbusillo/disable_odoo_online", ref="main"),
                    ),
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-private",
                        digest="sha256:image456",
                        tags=("sha-f45db648",),
                    ),
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "artifacts",
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--local-rehearsal",
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            persisted_manifest = state_dir / "artifacts" / "artifact-sha256-image456.json"
            self.assertTrue(persisted_manifest.exists())

    def test_backup_gates_write_and_show(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "backup-gate.json"
            input_file.write_text(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-10T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={
                        "snapshot": "opw-predeploy-20260410-182231",
                        "storage": "pbs",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            write_result = runner.invoke(
                main,
                [
                    "backup-gates",
                    "write",
                    "--state-dir",
                    str(state_dir),
                    "--local-rehearsal",
                    "--input-file",
                    str(input_file),
                ],
            )
            show_result = runner.invoke(
                main,
                [
                    "backup-gates",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    "backup-opw-prod-20260410T182231Z",
                ],
            )

            self.assertEqual(write_result.exit_code, 0, msg=write_result.output)
            self.assertEqual(show_result.exit_code, 0, msg=show_result.output)
            self.assertIn('"snapshot": "opw-predeploy-20260410-182231"', show_result.output)

    def test_write_and_read_environment_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = EnvironmentInventory(
                context="opw",
                instance="prod",
                artifact_identity=_artifact_identity("artifact-20260410-f45db648"),
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
                post_deploy_update=_post_deploy_pass(),
                destination_health=_health_pass(),
                updated_at="2026-04-10T18:24:01Z",
                deployment_record_id="deployment-20260410T182231Z-opw-prod",
                promotion_record_id="promotion-20260410T182231Z-opw-testing-to-prod",
                promoted_from_instance="testing",
            )

            written_path = store.write_environment_inventory(record)
            loaded_record = store.read_environment_inventory(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_environment_inventory()

            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.context, "opw")
            self.assertEqual(loaded_record.instance, "prod")
            self.assertEqual(
                loaded_record.promotion_record_id, "promotion-20260410T182231Z-opw-testing-to-prod"
            )
            self.assertEqual(len(listed_records), 1)

    def test_write_and_read_odoo_instance_override_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = OdooInstanceOverrideRecord(
                context="opw",
                instance="prod",
                config_parameters=(
                    OdooConfigParameterOverride(
                        key="web.base.url",
                        value=OdooOverrideValue(
                            source="literal", value="https://opw-prod.example.com"
                        ),
                    ),
                ),
                addon_settings=(
                    OdooAddonSettingOverride(
                        addon="shopify",
                        setting="api_token",
                        value=OdooOverrideValue(
                            source="secret_binding",
                            secret_binding_id="secret-binding-shopify-token",
                        ),
                    ),
                ),
                website_bootstrap=OdooWebsiteBootstrapPayload(
                    tenant="opw",
                    name="OPW",
                    canonical_url="https://opw-prod.example.com",
                    logo_path="addons/opw/static/src/img/logo.png",
                    routes=(
                        OdooWebsiteBootstrapRoute(
                            name="Home",
                            url="/",
                            module="website",
                            homepage=True,
                        ),
                    ),
                ),
                updated_at="2026-04-21T18:30:00Z",
                source_label="test",
            )

            written_path = store.write_odoo_instance_override_record(record)
            loaded_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_odoo_instance_override_records()

            self.assertEqual(
                written_path.relative_to(state_dir).as_posix(),
                "odoo_instance_overrides/opw-prod.json",
            )
            self.assertEqual(
                loaded_record.addon_settings[0].value.secret_binding_id,
                "secret-binding-shopify-token",
            )
            self.assertIsNotNone(loaded_record.website_bootstrap)
            assert loaded_record.website_bootstrap is not None
            self.assertEqual(loaded_record.website_bootstrap.name, "OPW")
            self.assertEqual(loaded_record.website_bootstrap.routes[0].url, "/")
            self.assertEqual(
                [(record.context, record.instance) for record in listed_records], [("opw", "prod")]
            )
