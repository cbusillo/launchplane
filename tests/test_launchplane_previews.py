import os
import json
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane import cli as control_plane_cli
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationState,
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_record import PreviewRecord, PreviewState
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_profile_record import ProductImageProfile
from control_plane.contracts.product_profile_record import ProductPreviewProfile
from control_plane.contracts.preview_request_metadata import (
    LaunchplaneCompanionPullRequestReference,
    LaunchplanePreviewRequestParseStatus,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
    ReleaseStatus,
)
from control_plane.contracts.github_pull_request_event import PullRequestAction, PullRequestState
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import (
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    apply_preview_destroyed_transition,
    build_preview_canonical_url,
    build_preview_generation_record,
    build_preview_label,
    build_preview_record,
    build_preview_route_path,
    launchplane_anchor_repo_context,
    generate_preview_generation_id,
    generate_preview_id,
    resolve_launchplane_preview_base_url,
)
from control_plane.workflows.odoo_prod_backup_gate import (
    RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
)


CLI_MAIN = cast(Command, main)


def _preview_record(
    *,
    preview_id: str = "hpr_01jabc",
    context: str = "opw",
    anchor_repo: str = "tenant-opw",
    anchor_pr_number: int = 123,
    anchor_pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    preview_label: str = "opw/tenant-opw/pr-123",
    canonical_url: str = "https://launchplane.example/previews/opw/tenant-opw/pr-123",
    state: PreviewState = "active",
    active_generation_id: str = "hgen_01jabc_1",
    serving_generation_id: str = "hgen_01jabc_1",
    latest_generation_id: str = "hgen_01jabc_1",
    latest_manifest_fingerprint: str = "launchplane-manifest-001",
    created_at: str = "2026-04-13T12:00:00Z",
    updated_at: str = "2026-04-13T12:14:00Z",
    eligible_at: str = "2026-04-13T12:00:00Z",
    paused_at: str = "",
    destroy_after: str = "2026-04-20T12:14:00Z",
    destroyed_at: str = "",
    destroy_reason: str = "",
) -> PreviewRecord:
    return PreviewRecord(
        preview_id=preview_id,
        context=context,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        preview_label=preview_label,
        canonical_url=canonical_url,
        state=state,
        created_at=created_at,
        updated_at=updated_at,
        eligible_at=eligible_at,
        paused_at=paused_at,
        destroy_after=destroy_after,
        destroyed_at=destroyed_at,
        destroy_reason=destroy_reason,
        active_generation_id=active_generation_id,
        serving_generation_id=serving_generation_id,
        latest_generation_id=latest_generation_id,
        latest_manifest_fingerprint=latest_manifest_fingerprint,
    )


def _preview_product_profile(
    *,
    product: str,
    repository: str,
    preview_context: str,
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name=product.title(),
        repository=repository,
        driver_id="generic-web",
        image=ProductImageProfile(repository=f"ghcr.io/{repository}"),
        runtime_port=3000,
        health_path="/health",
        preview=ProductPreviewProfile(
            enabled=True,
            context=preview_context,
        ),
        updated_at="2026-05-09T00:00:00Z",
        source="test",
    )


def _write_opw_preview_product_profile(store: FilesystemRecordStore) -> None:
    store.write_product_profile_record(
        _preview_product_profile(
            product="tenant-opw",
            repository="every/tenant-opw",
            preview_context="opw",
        )
    )


def _write_cm_preview_product_profile(store: FilesystemRecordStore) -> None:
    store.write_product_profile_record(
        _preview_product_profile(
            product="tenant-cm",
            repository="every/tenant-cm",
            preview_context="cm",
        )
    )


def _write_default_preview_product_profiles(store: FilesystemRecordStore) -> None:
    _write_opw_preview_product_profile(store)
    _write_cm_preview_product_profile(store)


def _generation_record(
    generation_id: str,
    *,
    preview_id: str = "hpr_01jabc",
    anchor_repo: str = "tenant-opw",
    anchor_pr_number: int = 123,
    anchor_pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    anchor_head_sha: str = "aaaa1111",
    sequence: int,
    state: PreviewGenerationState,
    manifest_fingerprint: str,
    artifact_id: str,
    deploy_status: ReleaseStatus = "pass",
    verify_status: ReleaseStatus = "pass",
    overall_health_status: ReleaseStatus = "pass",
    failure_stage: str = "",
    failure_summary: str = "",
    ready_at: str = "2026-04-13T12:12:00Z",
    failed_at: str = "",
) -> PreviewGenerationRecord:
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=sequence,
        state=state,
        requested_reason="manifest_changed" if sequence > 1 else "initial_create",
        requested_at="2026-04-13T12:10:00Z",
        started_at="2026-04-13T12:10:03Z",
        ready_at=ready_at,
        failed_at=failed_at,
        expires_at="2026-04-20T12:14:00Z",
        resolved_manifest_fingerprint=manifest_fingerprint,
        artifact_id=artifact_id,
        baseline_release_tuple_id="opw-testing-2026-04-13",
        source_map=(
            PreviewSourceRecord(repo=anchor_repo, git_sha=anchor_head_sha, selection="anchor"),
            PreviewSourceRecord(repo="shared-addons", git_sha="bbbb2222", selection="companion"),
        ),
        anchor_summary=PreviewPullRequestSummary(
            repo=anchor_repo,
            pr_number=anchor_pr_number,
            head_sha=anchor_head_sha,
            pr_url=anchor_pr_url,
        ),
        companion_summaries=(
            PreviewPullRequestSummary(
                repo="shared-addons",
                pr_number=456,
                head_sha="bbbb2222",
                pr_url="https://github.com/every/shared-addons/pull/456",
            ),
        ),
        deploy_status=deploy_status,
        verify_status=verify_status,
        overall_health_status=overall_health_status,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
    )


def _environment_inventory(
    *,
    context: str = "opw",
    instance: str,
    artifact_id: str,
    source_git_ref: str,
    updated_at: str,
    deployment_record_id: str,
    promoted_from_instance: str = "",
) -> EnvironmentInventory:
    return EnvironmentInventory(
        context=context,
        instance=instance,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        source_git_ref=source_git_ref,
        deploy=DeploymentEvidence(
            target_name=f"{context}-{instance}",
            target_type="compose",
            deploy_mode="dokploy",
            deployment_id=f"deploy-{instance}",
            status="pass",
            started_at="2026-04-14T11:00:00Z",
            finished_at="2026-04-14T11:03:00Z",
        ),
        destination_health=HealthcheckEvidence(status="pass"),
        updated_at=updated_at,
        deployment_record_id=deployment_record_id,
        promoted_from_instance=promoted_from_instance,
    )


def _preview_enablement_record(
    *,
    context: str = "opw",
    anchor_repo: str = "tenant-opw",
    anchor_pr_number: int = 123,
    anchor_pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    anchor_head_sha: str = "aaaa1111",
    action: PullRequestAction = "opened",
    pr_state: PullRequestState = "open",
    updated_at: str = "2026-04-14T11:15:00Z",
    request_metadata_status: LaunchplanePreviewRequestParseStatus = "missing",
    request_metadata_error: str = "",
    request_metadata_baseline_channel: str = "",
    request_metadata_companions: tuple[LaunchplaneCompanionPullRequestReference, ...] = (),
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...] = (),
) -> PreviewEnablementRecord:
    return PreviewEnablementRecord(
        record_id=f"{context}-{anchor_repo}-pr-{anchor_pr_number}",
        context=context,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        anchor_head_sha=anchor_head_sha,
        action=action,
        pr_state=pr_state,
        updated_at=updated_at,
        request_metadata_status=request_metadata_status,
        request_metadata_error=request_metadata_error,
        request_metadata_baseline_channel=request_metadata_baseline_channel,
        request_metadata_companions=request_metadata_companions,
        request_metadata_companion_summaries=request_metadata_companion_summaries,
    )


def _backup_gate_record(
    *,
    record_id: str = "backup-opw-prod-20260414T111500Z",
    context: str = "opw",
    instance: str = "prod",
    created_at: str = "2026-04-14T11:15:00Z",
    source: str = "prod-gate",
    required: bool = True,
    status: ReleaseStatus = "pass",
    evidence: dict[str, str] | None = None,
) -> BackupGateRecord:
    resolved_evidence = (
        evidence if evidence is not None else {"snapshot": "s3://launchplane/opw/prod/2026-04-14"}
    )
    return BackupGateRecord(
        record_id=record_id,
        context=context,
        instance=instance,
        created_at=created_at,
        source=source,
        required=required,
        status=status,
        evidence=resolved_evidence,
    )


def _promotion_record(
    *,
    record_id: str = "promotion-2026-04-13T09:00:00Z-opw-testing-to-prod",
    artifact_id: str = "artifact-prod",
    backup_record_id: str = "backup-opw-prod-20260413T085500Z",
    context: str = "opw",
    from_instance: str = "testing",
    to_instance: str = "prod",
    deploy_status: ReleaseStatus = "pass",
    destination_health_status: ReleaseStatus = "pass",
) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        backup_record_id=backup_record_id,
        context=context,
        from_instance=from_instance,
        to_instance=to_instance,
        source_health=HealthcheckEvidence(status="pass"),
        backup_gate=BackupGateEvidence(
            required=True,
            status="pass",
            evidence={"backup_record_id": backup_record_id},
        ),
        deploy=DeploymentEvidence(
            target_name=f"{context}-{to_instance}",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="deployment-prod-promotion",
            status=deploy_status,
            started_at="2026-04-13T08:56:00Z",
            finished_at="2026-04-13T09:00:00Z",
        ),
        post_deploy_update=PostDeployUpdateEvidence(
            attempted=True, status="pass", detail="Updated"
        ),
        destination_health=HealthcheckEvidence(status=destination_health_status),
    )


def _write_release_tuples_file(control_plane_root: Path) -> None:
    _write_default_preview_product_profiles(
        FilesystemRecordStore(state_dir=control_plane_root / "state")
    )
    database_url = _runtime_environments_database_url(control_plane_root)
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="opw-testing-2026-04-13",
                context="opw",
                channel="testing",
                artifact_id="artifact-opw-testing",
                repo_shas={
                    "tenant-opw": "1111111111111111111111111111111111111111",
                    "shared-addons": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                },
                provenance="ship",
                minted_at="2026-04-22T00:00:00Z",
            )
        )
        store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id="cm-testing-2026-04-13",
                context="cm",
                channel="testing",
                artifact_id="artifact-cm-testing",
                repo_shas={
                    "tenant-cm": "3333333333333333333333333333333333333333",
                    "shared-addons": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                },
                provenance="ship",
                minted_at="2026-04-22T00:00:00Z",
            )
        )
    finally:
        store.close()


def _write_runtime_environments_file(control_plane_root: Path, payload: str | None = None) -> None:
    if payload is None:
        payload = """
schema_version = 1

[shared_env]
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"
GITHUB_WEBHOOK_SECRET = "launchplane-webhook-secret"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.cm.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
    database_url = _runtime_environments_database_url(control_plane_root)
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        for (
            record
        ) in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
            control_plane_runtime_environments._parse_runtime_environment_definition(
                tomllib.loads(payload),
                source_file=control_plane_root / "config" / "runtime-environments.toml",
            ),
            updated_at="2026-04-22T00:00:00Z",
            source_label="test",
        ):
            store.write_runtime_environment_record(record)
    finally:
        store.close()


def _runtime_environments_database_url(control_plane_root: Path) -> str:
    return f"sqlite+pysqlite:///{control_plane_root / 'launchplane.sqlite3'}"


def _runtime_environments_env(control_plane_root: Path) -> dict[str, str]:
    return {"LAUNCHPLANE_DATABASE_URL": _runtime_environments_database_url(control_plane_root)}


class LaunchplanePreviewReadModelTests(unittest.TestCase):
    def test_launchplane_preview_enablement_item_helpers_classify_states(
        self,
    ) -> None:
        running_preview: dict[str, object] = {
            "state": "active",
            "serving_generation_id": "generation-1",
        }
        paused_preview: dict[str, object] = {
            "state": "paused",
            "serving_generation_id": "generation-1",
        }
        destroyed_preview: dict[str, object] = {
            "state": "destroyed",
            "serving_generation_id": "",
        }

        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_state(
                preview_row=running_preview,
                pr_state="open",
            ),
            "running",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_state(
                preview_row=paused_preview,
                pr_state="open",
            ),
            "paused",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_state(
                preview_row=destroyed_preview,
                pr_state="closed",
            ),
            "retained",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_state(
                preview_row=None,
                pr_state="open",
            ),
            "candidate",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_tone("running"), "good"
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_tone("requested"),
            "warn",
        )

    def test_launchplane_preview_enablement_item_helpers_summarize_sources(
        self,
    ) -> None:
        preview_row: dict[str, object] = {
            "state": "active",
            "serving_generation_id": "generation-1",
        }

        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_source(
                preview_row=preview_row,
                latest_requested_reason="",
            ),
            "history",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_source(
                preview_row=preview_row,
                latest_requested_reason="operator_requested_enablement",
            ),
            "launchplane",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_status_summary(
                state="candidate",
                preview_row=None,
            ),
            "Ready for opt-in preview enablement.",
        )
        self.assertEqual(
            control_plane_cli._launchplane_preview_enablement_item_status_summary(
                state="running",
                preview_row={"status_summary": "Preview is serving traffic."},
            ),
            "Preview is serving traffic.",
        )

    def test_launchplane_preview_identity_helpers_are_deterministic(self) -> None:
        self.assertEqual(
            build_preview_label(
                context_name="opw",
                anchor_repo="tenant-opw",
                anchor_pr_number=123,
            ),
            "opw/tenant-opw/pr-123",
        )
        self.assertEqual(
            build_preview_route_path(
                context_name="opw",
                anchor_repo="tenant-opw",
                anchor_pr_number=123,
            ),
            "/previews/opw/tenant-opw/pr-123",
        )
        self.assertEqual(
            generate_preview_id(
                context_name="opw",
                anchor_repo="tenant-opw",
                anchor_pr_number=123,
            ),
            "preview-opw-tenant-opw-pr-123",
        )
        self.assertEqual(
            generate_preview_generation_id(
                preview_id="preview-opw-tenant-opw-pr-123",
                sequence=2,
            ),
            "preview-opw-tenant-opw-pr-123-generation-0002",
        )
        self.assertEqual(
            build_preview_canonical_url(
                preview_base_url="https://launchplane.example",
                context_name="opw",
                anchor_repo="tenant-opw",
                anchor_pr_number=123,
            ),
            "https://launchplane.example/previews/opw/tenant-opw/pr-123",
        )

    def test_build_preview_record_reuses_stable_identity_for_same_anchor(self) -> None:
        first_record = build_preview_record(
            context_name="opw",
            anchor_repo="tenant-opw",
            anchor_pr_number=123,
            anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
            created_at="2026-04-13T12:00:00Z",
            updated_at="2026-04-13T12:10:00Z",
            preview_base_url="https://launchplane.example",
            state="active",
        )
        reopened_record = build_preview_record(
            context_name="opw",
            anchor_repo="tenant-opw",
            anchor_pr_number=123,
            anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
            created_at="2026-04-14T09:00:00Z",
            updated_at="2026-04-14T09:05:00Z",
            preview_base_url="https://launchplane.example",
            state="pending",
        )

        self.assertEqual(first_record.preview_id, reopened_record.preview_id)
        self.assertEqual(first_record.preview_label, reopened_record.preview_label)
        self.assertEqual(first_record.canonical_url, reopened_record.canonical_url)

    def test_resolve_launchplane_preview_base_url_reads_context_runtime_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[shared_env]
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip(),
            )

            with patch.dict(os.environ, _runtime_environments_env(control_plane_root), clear=True):
                resolved_base_url = resolve_launchplane_preview_base_url(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                )

        self.assertEqual(resolved_base_url, "https://launchplane.example")

    def test_resolve_launchplane_preview_base_url_fails_closed_when_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip(),
            )

            with patch.dict(os.environ, _runtime_environments_env(control_plane_root), clear=True):
                with self.assertRaisesRegex(Exception, "LAUNCHPLANE_PREVIEW_BASE_URL"):
                    resolve_launchplane_preview_base_url(
                        control_plane_root=control_plane_root,
                        context_name="opw",
                    )

    def test_build_preview_generation_record_links_anchor_and_sequence(self) -> None:
        runtime_identity = RuntimeIdentity(
            product="odoo-tenant-opw",
            context="opw-preview",
            instance="pr-123",
            environment_kind="preview",
            deployment_record_id="deployment-preview-123",
            artifact_id="artifact-opw-124",
            source_git_ref="aaaa1111",
            preview_id="preview-opw-tenant-opw-pr-123",
        )
        generation_record = build_preview_generation_record(
            preview_id="preview-opw-tenant-opw-pr-123",
            sequence=2,
            state="failed",
            requested_reason="manifest_changed",
            requested_at="2026-04-13T12:10:00Z",
            resolved_manifest_fingerprint="launchplane-manifest-002",
            anchor_repo="tenant-opw",
            anchor_pr_number=123,
            anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
            anchor_head_sha="aaaa1111",
            artifact_id="artifact-opw-124",
            deploy_status="fail",
            verify_status="skipped",
            overall_health_status="fail",
            failure_stage="deploying",
            failure_summary="Replacement generation failed during deploy.",
            runtime_identity=runtime_identity,
        )

        self.assertEqual(
            generation_record.generation_id,
            "preview-opw-tenant-opw-pr-123-generation-0002",
        )
        self.assertEqual(generation_record.anchor_summary.repo, "tenant-opw")
        self.assertEqual(generation_record.anchor_summary.pr_number, 123)
        self.assertEqual(generation_record.failure_stage, "deploying")
        self.assertEqual(generation_record.runtime_identity, runtime_identity)

    def test_apply_generation_requested_transition_keeps_existing_serving_generation(self) -> None:
        preview = _preview_record(
            state="active",
            active_generation_id="hgen_01jabc_1",
            serving_generation_id="hgen_01jabc_1",
            latest_generation_id="hgen_01jabc_1",
        )
        generation = _generation_record(
            "hgen_01jabc_2",
            sequence=2,
            state="building",
            manifest_fingerprint="launchplane-manifest-002",
            artifact_id="artifact-opw-124",
            ready_at="",
        )

        transitioned = apply_generation_requested_transition(
            preview=preview,
            generation=generation,
        )

        self.assertEqual(transitioned.state, "active")
        self.assertEqual(transitioned.active_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.latest_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.serving_generation_id, "hgen_01jabc_1")

    def test_apply_generation_ready_transition_cuts_over_serving_generation(self) -> None:
        preview = _preview_record(
            state="active",
            active_generation_id="hgen_01jabc_2",
            serving_generation_id="hgen_01jabc_1",
            latest_generation_id="hgen_01jabc_2",
        )
        generation = _generation_record(
            "hgen_01jabc_2",
            sequence=2,
            state="ready",
            manifest_fingerprint="launchplane-manifest-002",
            artifact_id="artifact-opw-124",
        )

        transitioned = apply_generation_ready_transition(
            preview=preview,
            generation=generation,
        )

        self.assertEqual(transitioned.state, "active")
        self.assertEqual(transitioned.active_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.serving_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.latest_generation_id, "hgen_01jabc_2")

    def test_apply_generation_failed_transition_keeps_older_serving_generation(self) -> None:
        preview = _preview_record(
            state="active",
            active_generation_id="hgen_01jabc_2",
            serving_generation_id="hgen_01jabc_1",
            latest_generation_id="hgen_01jabc_2",
        )
        generation = _generation_record(
            "hgen_01jabc_2",
            sequence=2,
            state="failed",
            manifest_fingerprint="launchplane-manifest-002",
            artifact_id="artifact-opw-124",
            failed_at="2026-04-13T12:16:00Z",
        )

        transitioned = apply_generation_failed_transition(
            preview=preview,
            generation=generation,
        )

        self.assertEqual(transitioned.state, "failed")
        self.assertEqual(transitioned.active_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.latest_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.serving_generation_id, "hgen_01jabc_1")

    def test_apply_preview_destroyed_transition_clears_runtime_links_and_keeps_evidence(
        self,
    ) -> None:
        preview = _preview_record(
            state="teardown_pending",
            active_generation_id="hgen_01jabc_2",
            serving_generation_id="hgen_01jabc_1",
            latest_generation_id="hgen_01jabc_2",
        )

        transitioned = apply_preview_destroyed_transition(
            preview=preview,
            destroyed_at="2026-04-14T12:14:00Z",
            destroy_reason="merged_after_grace_window",
        )

        self.assertEqual(transitioned.state, "destroyed")
        self.assertEqual(transitioned.active_generation_id, "")
        self.assertEqual(transitioned.serving_generation_id, "")
        self.assertEqual(transitioned.latest_generation_id, "hgen_01jabc_2")
        self.assertEqual(transitioned.destroy_reason, "merged_after_grace_window")

    def test_launchplane_anchor_repo_resolution_accepts_tenant_repos_only(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_product_profile_record(
                _preview_product_profile(
                    product="tenant-opw",
                    repository="every/tenant-opw",
                    preview_context="opw",
                )
            )
            store.write_product_profile_record(
                _preview_product_profile(
                    product="tenant-cm",
                    repository="every/tenant-cm",
                    preview_context="cm",
                )
            )

            self.assertEqual(
                launchplane_anchor_repo_context(record_store=store, repo="tenant-opw"), "opw"
            )
            self.assertEqual(
                launchplane_anchor_repo_context(record_store=store, repo="tenant-cm"), "cm"
            )
            self.assertEqual(
                launchplane_anchor_repo_context(record_store=store, repo="shared-addons"), ""
            )
            self.assertEqual(
                launchplane_anchor_repo_context(record_store=store, repo="control-plane"), ""
            )

    def test_filesystem_store_lists_preview_records_and_generations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record())
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="pending",
                    verify_status="pending",
                    overall_health_status="pending",
                    ready_at="",
                )
            )

            previews = store.list_preview_records(context_name="opw", anchor_repo="tenant-opw")
            generations = store.list_preview_generation_records(preview_id="hpr_01jabc")

            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0].preview_label, "opw/tenant-opw/pr-123")
            self.assertEqual(
                [record.generation_id for record in generations],
                [
                    "hgen_01jabc_2",
                    "hgen_01jabc_1",
                ],
            )

    def test_launchplane_previews_show_active_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record())
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["preview"]["preview_label"], "opw/tenant-opw/pr-123")
            self.assertEqual(payload["trust_summary"]["artifact_id"], "artifact-opw-123")
            self.assertTrue(payload["health_summary"]["serving_matches_latest"])
            self.assertEqual(
                payload["health_summary"]["status_summary"],
                "Serving the latest requested generation.",
            )

    def test_launchplane_previews_show_surfaces_first_page_summary_fields(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_1",
                    destroy_after="2026-04-20T12:10:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(
                payload["preview"]["canonical_url"],
                "https://launchplane.example/previews/opw/tenant-opw/pr-123",
            )
            self.assertEqual(payload["preview"]["preview_label"], "opw/tenant-opw/pr-123")
            self.assertEqual(payload["trust_summary"]["artifact_id"], "artifact-opw-123")
            self.assertEqual(
                payload["trust_summary"]["manifest_fingerprint"],
                "launchplane-manifest-001",
            )
            self.assertEqual(
                payload["lifecycle_summary"]["next_action"],
                "Launchplane will keep this preview until the current destroy-after deadline or a lifecycle event replaces it.",
            )
            self.assertEqual(
                payload["input_summary"]["source_map"][0]["repo"],
                "tenant-opw",
            )

    def test_launchplane_previews_render_status_page_writes_html_summary(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_1",
                    destroy_after="2026-04-20T12:10:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Launchplane control plane", rendered_html)
            self.assertIn("Preview detail", rendered_html)
            self.assertIn('class="preview-detail-mast"', rendered_html)
            self.assertIn("Current preview evidence", rendered_html)
            self.assertIn('class="preview-detail-grid"', rendered_html)
            self.assertIn("tenant-opw PR 123", rendered_html)
            self.assertIn("opw/tenant-opw/pr-123", rendered_html)
            self.assertIn(
                "https://launchplane.example/previews/opw/tenant-opw/pr-123", rendered_html
            )
            self.assertIn("artifact-opw-123", rendered_html)
            self.assertIn("launchplane-manifest-001", rendered_html)
            self.assertIn("Serving the latest requested generation.", rendered_html)
            self.assertIn("Write-side Launchplane recipes", rendered_html)
            self.assertIn("request-generation", rendered_html)
            self.assertIn("destroy-preview", rendered_html)
            self.assertIn("--local-rehearsal", rendered_html)
            self.assertIn('id="operator-actions"', rendered_html)
            self.assertIn(
                "This preview is live at the stable Launchplane route and serving the latest requested generation.",
                rendered_html,
            )
            self.assertIn("Raw payload JSON", rendered_html)
            self.assertIn("Open preview URL", rendered_html)
            self.assertIn("serving / latest", rendered_html)

    def test_launchplane_previews_show_tenant_surfaces_environment_and_preview_lanes(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _write_opw_preview_product_profile(store)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@zzz999",
                    updated_at="2026-04-13T09:00:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    active_generation_id="hgen_live_1",
                    serving_generation_id="hgen_live_1",
                    latest_generation_id="hgen_live_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_live_1",
                    preview_id="hpr_live",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_pending",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-124",
                    state="pending",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="",
                    latest_manifest_fingerprint="",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    action="labeled",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=125,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/125",
                    anchor_head_sha="bbbb2222",
                    updated_at="2026-04-14T11:16:00Z",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_launchplane",
                    anchor_pr_number=126,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/126",
                    preview_label="opw/tenant-opw/pr-126",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-126",
                    active_generation_id="hgen_launchplane_1",
                    serving_generation_id="hgen_launchplane_1",
                    latest_generation_id="hgen_launchplane_1",
                    updated_at="2026-04-14T11:18:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_launchplane_1",
                    preview_id="hpr_launchplane",
                    anchor_pr_number=126,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/126",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-launchplane",
                    artifact_id="artifact-launchplane",
                ).model_copy(update={"requested_reason": "operator_requested_refresh"})
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show-tenant",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["context"], "opw")
            self.assertEqual(payload["anchor_repo"], "tenant-opw")
            self.assertEqual(payload["preview_counts"]["live"], 2)
            self.assertEqual(payload["preview_counts"]["in_flight"], 1)
            self.assertEqual(len(payload["preview_candidates"]), 1)
            self.assertEqual(payload["preview_enablement_counts"]["candidate"], 1)
            self.assertEqual(payload["preview_enablement_counts"]["requested"], 1)
            self.assertEqual(payload["preview_enablement_counts"]["running"], 2)
            self.assertEqual(
                payload["environments"]["testing"]["live"]["artifact_id"], "artifact-testing"
            )
            self.assertEqual(
                payload["environments"]["prod"]["live"]["artifact_id"], "artifact-prod"
            )
            self.assertEqual(payload["promotion_summary"]["status"], "candidate")
            self.assertEqual(payload["promotion_action"]["status"], "blocked")
            self.assertEqual(payload["environment_actions"]["testing"]["status"], "actionable")
            self.assertEqual(payload["environment_actions"]["prod"]["status"], "actionable")
            self.assertIn("ship resolve", payload["environment_actions"]["testing"]["recipe"])
            self.assertIn("--local-rehearsal", payload["environment_actions"]["testing"]["recipe"])
            self.assertNotIn(
                "--allow-direct-db-mutation",
                payload["environment_actions"]["testing"]["recipe"],
            )
            self.assertNotIn(
                "LAUNCHPLANE_DATABASE_URL",
                payload["environment_actions"]["testing"]["recipe"],
            )
            self.assertEqual(
                payload["promotion_action"]["candidate_artifact_id"], "artifact-testing"
            )
            self.assertEqual(
                payload["promotion_action"]["current_prod_artifact_id"], "artifact-prod"
            )
            self.assertEqual(
                payload["promotion_action"]["evidence_checks"][3]["label"], "Prod backup gate"
            )
            self.assertIn(
                "no prod backup-gate evidence",
                payload["promotion_action"]["evidence_checks"][3]["detail"].lower(),
            )
            self.assertIn("backup-gates write", payload["promotion_action"]["backup_gate_recipe"])
            self.assertEqual(payload["promotion_action"]["resolve_recipe"], "")
            self.assertEqual(payload["promotion_action"]["execute_recipe"], "")
            self.assertEqual(payload["promotion_detail"]["status"], "blocked")
            self.assertEqual(payload["promotion_detail"]["from_instance"], "testing")
            self.assertEqual(payload["promotion_detail"]["to_instance"], "prod")
            enablement_by_pr = {
                item["anchor_pr_number"]: item for item in payload["preview_enablement"]
            }
            self.assertEqual(enablement_by_pr[125]["state"], "candidate")
            self.assertEqual(enablement_by_pr[125]["request_source"], "none")
            self.assertEqual(enablement_by_pr[125]["action"]["status"], "actionable")
            self.assertIn("request-generation", enablement_by_pr[125]["action"]["recipe"])
            self.assertEqual(enablement_by_pr[124]["state"], "requested")
            self.assertEqual(enablement_by_pr[124]["request_source"], "history")
            self.assertEqual(enablement_by_pr[124]["action"]["status"], "existing_preview")
            self.assertEqual(enablement_by_pr[126]["state"], "running")
            self.assertEqual(enablement_by_pr[126]["request_source"], "launchplane")
            self.assertEqual(enablement_by_pr[126]["action"]["status"], "existing_preview")

    def test_launchplane_promotion_backup_gate_check_records_failed_gate(self) -> None:
        evidence_check = control_plane_cli._launchplane_promotion_backup_gate_evidence_check(
            _backup_gate_record(status="fail")
        )

        self.assertEqual(evidence_check["label"], "Prod backup gate")
        self.assertEqual(evidence_check["status"], "fail")
        self.assertIn("failed", evidence_check["detail"])

    def test_launchplane_promotion_backup_gate_check_rejects_recovery_import(self) -> None:
        evidence_check = control_plane_cli._launchplane_promotion_backup_gate_evidence_check(
            _backup_gate_record(source=RETAINED_VOLUME_BACKUP_IMPORT_SOURCE)
        )

        self.assertEqual(evidence_check["label"], "Prod backup gate")
        self.assertEqual(evidence_check["status"], "pending")
        self.assertIn("recovery-only", evidence_check["detail"])

    def test_launchplane_previews_write_enablement_persists_typed_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            input_file = Path(temporary_directory_name) / "preview-enablement.json"
            input_file.write_text(
                json.dumps(
                    _preview_enablement_record(
                        anchor_pr_number=131,
                        anchor_pr_url="https://github.com/every/tenant-opw/pull/131",
                        anchor_head_sha="eeee5555",
                        action="labeled",
                        request_metadata_status="valid",
                        request_metadata_baseline_channel="testing",
                    ).model_dump(mode="json")
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "write-enablement",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("opw-tenant-opw-pr-131", result.output)
            record = FilesystemRecordStore(state_dir=state_dir).read_preview_enablement_record(
                "opw-tenant-opw-pr-131"
            )
            self.assertEqual(record.request_metadata_status, "valid")
            self.assertEqual(record.request_metadata_baseline_channel, "testing")

    def test_launchplane_previews_show_tenant_uses_valid_metadata_snapshot_for_enablement_actions(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=129,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/129",
                    anchor_head_sha="dddd4444",
                    action="labeled",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show-tenant",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            enablement_by_pr = {
                item["anchor_pr_number"]: item for item in payload["preview_enablement"]
            }
            self.assertEqual(enablement_by_pr[129]["request_metadata_status"], "valid")
            self.assertEqual(enablement_by_pr[129]["action"]["status"], "actionable")

    def test_launchplane_previews_render_site_release_tuple_records_resolve_enablement_recipe(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            state_dir = temporary_directory / "state"
            output_dir = temporary_directory / "site"
            _write_release_tuples_file(temporary_directory)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=130,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/130",
                    anchor_head_sha="dddd4444",
                    action="labeled",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
                env=_runtime_environments_env(temporary_directory),
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("opw-testing-2026-04-13", index_html)
            self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", index_html)
            self.assertNotIn("&lt;resolved-baseline-tuple-id&gt;", index_html)
            self.assertNotIn("&lt;resolved-manifest-fingerprint&gt;", index_html)

    def test_launchplane_previews_render_index_page_leads_with_tenant_environment_when_scoped(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@zzz999",
                    updated_at="2026-04-13T09:00:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    active_generation_id="hgen_live_1",
                    serving_generation_id="hgen_live_1",
                    latest_generation_id="hgen_live_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_live_1",
                    preview_id="hpr_live",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    action="labeled",
                    updated_at="2026-04-14T11:16:00Z",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=125,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/125",
                    anchor_head_sha="bbbb2222",
                    updated_at="2026-04-14T11:17:00Z",
                )
            )
            store.write_backup_gate_record(
                _backup_gate_record(
                    record_id="backup-opw-prod-20260414T111500Z",
                    created_at="2026-04-14T11:15:00Z",
                )
            )
            store.write_promotion_record(
                _promotion_record(
                    record_id="promotion-2026-04-13T09:00:00Z-opw-testing-to-prod",
                    artifact_id="artifact-prod",
                    backup_record_id="backup-opw-prod-20260413T085500Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Tenant environment", rendered_html)
            self.assertIn("Testing lane", rendered_html)
            self.assertIn("Prod lane", rendered_html)
            self.assertIn("Main feeds testing.", rendered_html)
            self.assertIn(
                "Testing is carrying a newer artifact than prod and is the current promotion candidate.",
                rendered_html,
            )
            self.assertIn("Testing is ready to promote into prod.", rendered_html)
            self.assertIn("Rebuild long-lived lanes", rendered_html)
            self.assertIn("Re-ship current testing artifact", rendered_html)
            self.assertIn("ship resolve -&gt; ship execute", rendered_html)
            self.assertIn("Promotion candidate", rendered_html)
            self.assertIn(
                "Latest prod backup gate backup-opw-prod-20260414T111500Z passed and can authorize promotion.",
                rendered_html,
            )
            self.assertIn("promote resolve", rendered_html)
            self.assertIn("promote execute", rendered_html)
            self.assertIn("--local-rehearsal", rendered_html)
            self.assertNotIn("--allow-direct-db-mutation", rendered_html)
            self.assertNotIn("LAUNCHPLANE_DATABASE_URL", rendered_html)
            self.assertNotIn("backup-gates write", rendered_html)
            self.assertIn("Why each PR does or does not have a preview", rendered_html)
            self.assertIn("Eligible tenant PR. No preview request is active yet.", rendered_html)
            self.assertIn(
                "Launchplane still has preview evidence from an earlier request.", rendered_html
            )
            self.assertIn("Request Launchplane preview", rendered_html)
            self.assertIn("Show Launchplane request recipe", rendered_html)
            self.assertIn("request-generation", rendered_html)
            self.assertIn("artifact-testing", rendered_html)
            self.assertIn("artifact-prod", rendered_html)
            self.assertIn("Pull request previews", rendered_html)

    def test_launchplane_previews_render_index_page_surfaces_backup_gate_recipe_when_promotion_blocked(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@zzz999",
                    updated_at="2026-04-13T09:00:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "A newer testing artifact exists, but Launchplane cannot promote it yet.",
                rendered_html,
            )
            self.assertIn("backup-gates write", rendered_html)
            self.assertIn("--local-rehearsal", rendered_html)
            self.assertNotIn("promote resolve", rendered_html)

    def test_launchplane_previews_render_index_page_leads_with_enablement_when_no_lane_evidence_exists(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=130,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/130",
                    anchor_head_sha="eeee5555",
                    action="labeled",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "Preview enablement is the first meaningful control surface", rendered_html
            )
            self.assertIn("Why each PR does or does not have a preview", rendered_html)
            self.assertIn("Request Launchplane preview", rendered_html)
            self.assertNotIn("Rebuild long-lived lanes", rendered_html)
            self.assertNotIn("Launchplane cannot plan the next promotion yet.", rendered_html)

    def test_launchplane_previews_render_index_page_marks_missing_lane_action_evidence(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Prod has no actionable ship evidence yet.", rendered_html)

    def test_launchplane_previews_show_tenant_marks_in_sync_promotion_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:10:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )
            store.write_backup_gate_record(
                _backup_gate_record(
                    record_id="backup-opw-prod-20260414T111500Z",
                    created_at="2026-04-14T11:15:00Z",
                )
            )
            store.write_promotion_record(
                _promotion_record(
                    record_id="promotion-2026-04-14T11:10:00Z-opw-testing-to-prod",
                    artifact_id="artifact-prod",
                    backup_record_id="backup-opw-prod-20260414T111500Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show-tenant",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["promotion_summary"]["status"], "in_sync")
            self.assertEqual(payload["promotion_action"]["status"], "in_sync")
            self.assertEqual(
                payload["promotion_action"]["headline"],
                "Prod is already serving the current testing artifact.",
            )
            self.assertEqual(payload["promotion_detail"]["status"], "in_sync")
            self.assertEqual(
                payload["promotion_detail"]["latest_backup_gate"]["record_id"],
                "backup-opw-prod-20260414T111500Z",
            )

    def test_launchplane_previews_render_index_page_writes_preview_dashboard(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-123",
                    active_generation_id="hgen_live_1",
                    serving_generation_id="hgen_live_1",
                    latest_generation_id="hgen_live_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_live_1",
                    preview_id="hpr_live",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_fail",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-124",
                    state="failed",
                    active_generation_id="hgen_fail_2",
                    serving_generation_id="hgen_fail_1",
                    latest_generation_id="hgen_fail_2",
                    latest_manifest_fingerprint="launchplane-manifest-fail",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_fail_1",
                    preview_id="hpr_fail",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-prev",
                    artifact_id="artifact-prev",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_fail_2",
                    preview_id="hpr_fail",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="launchplane-manifest-fail",
                    artifact_id="artifact-fail",
                    deploy_status="fail",
                    verify_status="skipped",
                    overall_health_status="fail",
                    failure_stage="deploying",
                    failure_summary="Replacement generation failed during deploy.",
                    ready_at="",
                    failed_at="2026-04-13T12:15:00Z",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_dead",
                    anchor_pr_number=125,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/125",
                    preview_label="opw/tenant-opw/pr-125",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-125",
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_dead_1",
                    destroyed_at="2026-04-14T12:14:00Z",
                    destroy_reason="merged_after_grace_window",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_dead_1",
                    preview_id="hpr_dead",
                    anchor_pr_number=125,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/125",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-dead",
                    artifact_id="artifact-dead",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Launchplane control plane", rendered_html)
            self.assertIn("Pull request previews", rendered_html)
            self.assertIn("Fleet focus", rendered_html)
            self.assertIn("Reviewable now", rendered_html)
            self.assertIn("Needs attention", rendered_html)
            self.assertIn("Live review", rendered_html)
            self.assertIn("Retained evidence", rendered_html)
            self.assertIn("Policy snapshot", rendered_html)
            self.assertIn('data-filter-control="attention"', rendered_html)
            self.assertIn("data-preview-row", rendered_html)
            self.assertIn("Serving older generation", rendered_html)
            self.assertIn("Evidence only", rendered_html)
            self.assertIn("opw/tenant-opw/pr-123", rendered_html)
            self.assertIn("opw/tenant-opw/pr-124", rendered_html)
            self.assertIn("opw/tenant-opw/pr-125", rendered_html)

    def test_launchplane_previews_render_index_page_surfaces_scope_controls_for_multi_context_inventory(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-index-all.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_opw",
                    context="opw",
                    anchor_repo="tenant-opw",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-123",
                    active_generation_id="hgen_opw_1",
                    serving_generation_id="hgen_opw_1",
                    latest_generation_id="hgen_opw_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_opw_1",
                    preview_id="hpr_opw",
                    anchor_repo="tenant-opw",
                    anchor_pr_number=123,
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-opw",
                    artifact_id="artifact-opw",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_cm",
                    context="cm",
                    anchor_repo="tenant-cm",
                    anchor_pr_number=88,
                    preview_label="cm/tenant-cm/pr-88",
                    canonical_url="https://launchplane.example/previews/cm/tenant-cm/pr-88",
                    active_generation_id="hgen_cm_1",
                    serving_generation_id="hgen_cm_1",
                    latest_generation_id="hgen_cm_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_cm_1",
                    preview_id="hpr_cm",
                    anchor_repo="tenant-cm",
                    anchor_pr_number=88,
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-cm",
                    artifact_id="artifact-cm",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-index-page",
                    "--state-dir",
                    str(state_dir),
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("All scopes", rendered_html)
            self.assertIn("Context cm", rendered_html)
            self.assertIn('data-scope-control="context:cm"', rendered_html)
            self.assertIn('data-scope-control="repo:tenant-cm"', rendered_html)
            self.assertIn('data-scopes="all context:cm repo:tenant-cm"', rendered_html)

    def test_launchplane_previews_render_policy_page_writes_contract_summary(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-policy.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            _write_default_preview_product_profiles(store)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    preview_label="opw/tenant-opw/pr-123",
                    active_generation_id="hgen_live_1",
                    serving_generation_id="hgen_live_1",
                    latest_generation_id="hgen_live_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_live_1",
                    preview_id="hpr_live",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-live",
                    artifact_id="artifact-live",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-policy-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Launchplane control plane", rendered_html)
            self.assertIn("How Launchplane decides what becomes a preview", rendered_html)
            self.assertIn("Preview follows the pull request", rendered_html)
            self.assertIn("shared-addons", rendered_html)
            self.assertIn("tenant-opw", rendered_html)
            self.assertIn("tenant-cm", rendered_html)
            self.assertIn("testing", rendered_html)

    def test_launchplane_previews_render_policy_page_shows_context_distribution_for_multi_context_inventory(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-policy-all.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            _write_default_preview_product_profiles(store)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_opw",
                    context="opw",
                    anchor_repo="tenant-opw",
                    preview_label="opw/tenant-opw/pr-123",
                    active_generation_id="hgen_opw_1",
                    serving_generation_id="hgen_opw_1",
                    latest_generation_id="hgen_opw_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_opw_1",
                    preview_id="hpr_opw",
                    anchor_repo="tenant-opw",
                    anchor_pr_number=123,
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-opw",
                    artifact_id="artifact-opw",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_cm",
                    context="cm",
                    anchor_repo="tenant-cm",
                    preview_label="cm/tenant-cm/pr-88",
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_cm_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_cm_1",
                    preview_id="hpr_cm",
                    anchor_repo="tenant-cm",
                    anchor_pr_number=88,
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-cm",
                    artifact_id="artifact-cm",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-policy-page",
                    "--state-dir",
                    str(state_dir),
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Context distribution", rendered_html)
            self.assertIn("all contexts", rendered_html)
            self.assertIn('href="index.html#scope=context:cm"', rendered_html)
            self.assertIn("<td>opw</td>", rendered_html)
            self.assertIn(">cm</a></td>", rendered_html)

    def test_launchplane_previews_render_site_writes_linked_bundle(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_dir = Path(temporary_directory_name) / "site"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    preview_label="opw/tenant-opw/pr-123",
                    active_generation_id="hgen_live_1",
                    serving_generation_id="hgen_live_1",
                    latest_generation_id="hgen_live_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_live_1",
                    preview_id="hpr_live",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-live",
                    artifact_id="artifact-live",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_file = output_dir / "index.html"
            policy_file = output_dir / "policy.html"
            detail_file = output_dir / "previews" / "opw" / "tenant-opw" / "pr-123.html"
            self.assertTrue(index_file.exists())
            self.assertTrue(policy_file.exists())
            self.assertTrue(detail_file.exists())
            index_html = index_file.read_text(encoding="utf-8")
            policy_html = policy_file.read_text(encoding="utf-8")
            detail_html = detail_file.read_text(encoding="utf-8")
            self.assertIn('href="previews/opw/tenant-opw/pr-123.html"', index_html)
            self.assertIn("#operator-actions", index_html)
            self.assertIn('href="policy.html"', index_html)
            self.assertIn('href="../../../index.html"', detail_html)
            self.assertIn('href="../../../policy.html"', detail_html)
            self.assertIn('href="previews/opw/tenant-opw/pr-123.html"', policy_html)

    def test_launchplane_previews_render_site_enablement_row_links_to_existing_preview_detail(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_dir = Path(temporary_directory_name) / "site"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_pending",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-124",
                    state="pending",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="",
                    latest_manifest_fingerprint="",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    action="labeled",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn(
                'href="https://github.com/every/tenant-opw/pull/124">PR</a><a href="previews/opw/tenant-opw/pr-124.html">Detail</a>',
                index_html,
            )

    def test_launchplane_previews_render_site_writes_environment_detail_pages_and_links_from_overview(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_dir = Path(temporary_directory_name) / "site"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@zzz999",
                    updated_at="2026-04-13T09:00:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )
            store.write_backup_gate_record(
                _backup_gate_record(
                    record_id="backup-opw-prod-20260414T111500Z",
                    created_at="2026-04-14T11:15:00Z",
                )
            )
            store.write_promotion_record(
                _promotion_record(
                    record_id="promotion-2026-04-14T11:10:00Z-opw-testing-to-prod",
                    artifact_id="artifact-prod",
                    backup_record_id="backup-opw-prod-20260414T111500Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_file = output_dir / "index.html"
            testing_detail_file = output_dir / "environments" / "opw" / "testing.html"
            prod_detail_file = output_dir / "environments" / "opw" / "prod.html"
            self.assertTrue(testing_detail_file.exists())
            self.assertTrue(prod_detail_file.exists())

            index_html = index_file.read_text(encoding="utf-8")
            testing_detail_html = testing_detail_file.read_text(encoding="utf-8")
            prod_detail_html = prod_detail_file.read_text(encoding="utf-8")

            self.assertIn('href="environments/opw/testing.html"', index_html)
            self.assertIn('href="environments/opw/prod.html"', index_html)
            self.assertIn("Open lane detail", index_html)
            self.assertIn('href="../../index.html"', testing_detail_html)
            self.assertIn('href="../../policy.html"', testing_detail_html)
            self.assertIn("Live lane snapshot", testing_detail_html)
            self.assertIn("Current environment evidence", testing_detail_html)
            self.assertIn("Recent promotions into this lane", prod_detail_html)
            self.assertIn("promotion-2026-04-14T11:10:00Z-opw-testing-to-prod", prod_detail_html)

    def test_launchplane_previews_render_site_environment_detail_marks_partial_evidence_cleanly(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_dir = Path(temporary_directory_name) / "site"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            testing_detail_file = output_dir / "environments" / "opw" / "testing.html"
            self.assertTrue(testing_detail_file.exists())

            testing_detail_html = testing_detail_file.read_text(encoding="utf-8")
            self.assertIn(
                "No live promotion record is attached to this lane inventory.", testing_detail_html
            )
            self.assertIn(
                "No authorized backup gate is attached to this lane yet.", testing_detail_html
            )
            self.assertIn("No deployment history recorded for this lane yet.", testing_detail_html)
            self.assertIn("No promotion history recorded into this lane yet.", testing_detail_html)

    def test_launchplane_previews_render_site_writes_promotion_detail_page_and_link_from_overview(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_dir = Path(temporary_directory_name) / "site"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_environment_inventory(
                _environment_inventory(
                    instance="testing",
                    artifact_id="artifact-testing",
                    source_git_ref="origin/main@abc123",
                    updated_at="2026-04-14T11:05:00Z",
                    deployment_record_id="deployment-testing",
                )
            )
            store.write_environment_inventory(
                _environment_inventory(
                    instance="prod",
                    artifact_id="artifact-prod",
                    source_git_ref="origin/main@zzz999",
                    updated_at="2026-04-13T09:00:00Z",
                    deployment_record_id="deployment-prod",
                    promoted_from_instance="testing",
                )
            )
            store.write_backup_gate_record(
                _backup_gate_record(
                    record_id="backup-opw-prod-20260414T111500Z",
                    created_at="2026-04-14T11:15:00Z",
                )
            )
            store.write_promotion_record(
                _promotion_record(
                    record_id="promotion-2026-04-14T11:10:00Z-opw-testing-to-prod",
                    artifact_id="artifact-prod",
                    backup_record_id="backup-opw-prod-20260414T111500Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_file = output_dir / "index.html"
            promotion_detail_file = output_dir / "promotions" / "opw" / "testing-to-prod.html"
            self.assertTrue(promotion_detail_file.exists())

            index_html = index_file.read_text(encoding="utf-8")
            promotion_detail_html = promotion_detail_file.read_text(encoding="utf-8")

            self.assertIn('href="promotions/opw/testing-to-prod.html"', index_html)
            self.assertIn("Open promotion detail", index_html)
            self.assertIn('href="../../index.html"', promotion_detail_html)
            self.assertIn('href="../../policy.html"', promotion_detail_html)
            self.assertIn("Recent promotions into prod", promotion_detail_html)
            self.assertIn("Recent prod backup authorization", promotion_detail_html)
            self.assertIn("promotion-detail-check-good", promotion_detail_html)
            self.assertIn("signal-chip signal-good", promotion_detail_html)
            self.assertIn(
                "promotion-2026-04-14T11:10:00Z-opw-testing-to-prod", promotion_detail_html
            )
            self.assertIn("promote execute", promotion_detail_html)
            self.assertIn("--local-rehearsal", promotion_detail_html)
            self.assertNotIn("--allow-direct-db-mutation", promotion_detail_html)
            self.assertNotIn("LAUNCHPLANE_DATABASE_URL", promotion_detail_html)

    def test_launchplane_promotion_status_html_uses_shared_status_tone_for_evidence(
        self,
    ) -> None:
        html = control_plane_cli._render_launchplane_promotion_status_page_html(
            {
                "context": "opw",
                "path_label": "opw/testing-to-prod",
                "evidence_checks": [
                    {
                        "label": "Deployment health",
                        "status": "healthy",
                        "detail": "Destination is serving.",
                    },
                    {
                        "label": "Backup gate",
                        "status": "failed",
                        "detail": "Backup gate failed.",
                    },
                ],
            }
        )

        self.assertIn("promotion-detail-check-good", html)
        self.assertIn("signal-chip signal-good", html)
        self.assertIn("promotion-detail-check-bad", html)
        self.assertIn("signal-chip signal-bad", html)
        self.assertNotIn("promotion-detail-check promotion-detail-check-warn", html)
        self.assertNotIn("signal-chip signal-warn", html)

    def test_launchplane_previews_render_status_page_calls_out_failed_latest_replacement(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="failed",
                    active_generation_id="hgen_01jabc_2",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_2",
                    latest_manifest_fingerprint="launchplane-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="fail",
                    verify_status="skipped",
                    overall_health_status="fail",
                    failure_stage="deploying",
                    failure_summary="Replacement generation failed during deploy.",
                    ready_at="",
                    failed_at="2026-04-13T12:15:00Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "Latest replacement failed. Launchplane is still serving the older preview.",
                rendered_html,
            )
            self.assertIn("Replacement generation failed during deploy.", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertIn("hgen_01jabc_2", rendered_html)

    def test_launchplane_previews_render_status_page_preserves_destroyed_preview_evidence(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_01jabc_1",
                    destroyed_at="2026-04-14T12:14:00Z",
                    destroy_reason="merged_after_grace_window",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "This preview has already been destroyed. Launchplane is retaining the record as evidence.",
                rendered_html,
            )
            self.assertIn("merged_after_grace_window", rendered_html)
            self.assertIn("2026-04-14T12:14:00Z", rendered_html)
            self.assertIn("Retained generation", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertIn("Open anchor pull request", rendered_html)
            self.assertIn("Retained preview URL", rendered_html)
            self.assertNotIn("Open preview URL", rendered_html)
            self.assertNotIn(
                "Latest replacement failed. Launchplane is still serving the older preview.",
                rendered_html,
            )

    def test_launchplane_previews_render_status_page_calls_out_paused_preview_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="paused",
                    paused_at="2026-04-14T16:20:00Z",
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "This preview is intentionally paused. Launchplane is holding the current review evidence in place.",
                rendered_html,
            )
            self.assertIn("2026-04-14T16:20:00Z", rendered_html)
            self.assertIn("Blocked until Launchplane resumes the preview.", rendered_html)
            self.assertIn("Open preview URL", rendered_html)
            self.assertNotIn(
                "This preview has already been destroyed. Launchplane is retaining the record as evidence.",
                rendered_html,
            )

    def test_launchplane_previews_render_status_page_calls_out_teardown_pending_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="teardown_pending",
                    destroy_after="2026-04-15T18:00:00Z",
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "This preview is queued for teardown. Launchplane is keeping the current runtime available until cleanup completes.",
                rendered_html,
            )
            self.assertIn("2026-04-15T18:00:00Z", rendered_html)
            self.assertIn(
                "Anchor PR and generation history remain after runtime cleanup.",
                rendered_html,
            )
            self.assertIn("Preview teardown pending", rendered_html)
            self.assertIn("Open preview URL", rendered_html)
            self.assertNotIn(
                "This preview is intentionally paused. Launchplane is holding the current review evidence in place.",
                rendered_html,
            )

    def test_launchplane_previews_render_status_page_calls_out_in_progress_replacement(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="active",
                    active_generation_id="hgen_01jabc_2",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_2",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="pending",
                    verify_status="pending",
                    overall_health_status="pending",
                    ready_at="",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "A replacement generation is in progress. Launchplane is still serving the current preview.",
                rendered_html,
            )
            self.assertIn("Current stage", rendered_html)
            self.assertIn("deploying", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertIn("2026-04-13T12:10:00Z", rendered_html)
            self.assertIn("latest / active", rendered_html)
            self.assertIn("mark-generation-ready", rendered_html)
            self.assertIn("mark-generation-failed", rendered_html)
            self.assertIn("--local-rehearsal", rendered_html)
            self.assertNotIn(
                "Latest replacement failed. Launchplane is still serving the older preview.",
                rendered_html,
            )

    def test_launchplane_previews_render_status_page_calls_out_no_generation_yet(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="pending",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="",
                    latest_manifest_fingerprint="",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "Launchplane has created this preview record, but the first generation has not been requested yet.",
                rendered_html,
            )
            self.assertIn("Preview route (not live yet)", rendered_html)
            self.assertIn("Open anchor pull request", rendered_html)
            self.assertIn("Latest generation", rendered_html)
            self.assertIn("Not created yet", rendered_html)
            self.assertNotIn("Open preview URL", rendered_html)

    def test_launchplane_previews_render_status_page_calls_out_no_serving_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "launchplane-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="active",
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="",
                    latest_generation_id="hgen_01jabc_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "render-status-page",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn(
                "Launchplane has generation evidence for this preview, but nothing is serving yet.",
                rendered_html,
            )
            self.assertIn("Preview route (not serving yet)", rendered_html)
            self.assertIn("Open anchor pull request", rendered_html)
            self.assertIn("Health unavailable", rendered_html)
            self.assertIn("Latest generation", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertNotIn("Open preview URL", rendered_html)

    def test_launchplane_previews_show_failed_latest_keeps_serving_generation(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="failed",
                    active_generation_id="hgen_01jabc_2",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_2",
                    latest_manifest_fingerprint="launchplane-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="fail",
                    verify_status="skipped",
                    overall_health_status="fail",
                    failure_stage="deploying",
                    failure_summary="Replacement generation failed during deploy.",
                    ready_at="",
                    failed_at="2026-04-13T12:15:00Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["preview"]["state"], "failed")
            self.assertEqual(payload["serving_generation"]["generation_id"], "hgen_01jabc_1")
            self.assertEqual(payload["latest_generation"]["generation_id"], "hgen_01jabc_2")
            self.assertFalse(payload["health_summary"]["serving_matches_latest"])
            self.assertIn("latest replacement failed", payload["health_summary"]["status_summary"])
            self.assertEqual(payload["recent_generations"][0]["generation_id"], "hgen_01jabc_2")

    def test_launchplane_previews_show_destroyed_preview_retains_evidence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_01jabc_1",
                    destroyed_at="2026-04-14T12:14:00Z",
                    destroy_reason="merged_after_grace_window",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["preview"]["state"], "destroyed")
            self.assertIsNone(payload["serving_generation"])
            self.assertEqual(payload["latest_generation"]["generation_id"], "hgen_01jabc_1")
            self.assertEqual(
                payload["lifecycle_summary"]["destroy_reason"],
                "merged_after_grace_window",
            )
            self.assertIn("destroyed", payload["health_summary"]["status_summary"].lower())

    def test_launchplane_previews_write_preview_creates_record_from_request(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[shared_env]
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip(),
            )
            input_file = control_plane_root / "preview-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "state": "pending",
                        "created_at": "2026-04-13T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "write-preview",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                    env=_runtime_environments_env(control_plane_root),
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            record = FilesystemRecordStore(state_dir=state_dir).read_preview_record(
                "preview-opw-tenant-opw-pr-123"
            )
            self.assertEqual(record.preview_label, "opw/tenant-opw/pr-123")
            self.assertEqual(
                record.canonical_url,
                "https://launchplane.example/previews/opw/tenant-opw/pr-123",
            )

    def test_launchplane_previews_write_preview_reuses_existing_identity_and_created_at(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[shared_env]
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip(),
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_legacy",
                    created_at="2026-04-10T10:00:00Z",
                    updated_at="2026-04-10T10:00:00Z",
                )
            )
            input_file = control_plane_root / "preview-update-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "state": "paused",
                        "updated_at": "2026-04-13T12:30:00Z",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "write-preview",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                    env=_runtime_environments_env(control_plane_root),
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            record = store.read_preview_record("hpr_legacy")
            self.assertEqual(record.preview_id, "hpr_legacy")
            self.assertEqual(record.created_at, "2026-04-10T10:00:00Z")
            self.assertEqual(record.updated_at, "2026-04-13T12:30:00Z")
            self.assertEqual(record.state, "paused")

    def test_launchplane_previews_write_preview_fails_closed_when_base_url_missing(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip(),
            )
            input_file = control_plane_root / "preview-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "state": "pending",
                        "created_at": "2026-04-13T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "write-preview",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                    env=_runtime_environments_env(control_plane_root),
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("LAUNCHPLANE_PREVIEW_BASE_URL", result.output)

    def test_launchplane_previews_write_preview_accepts_explicit_canonical_url_without_base_url(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "preview-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "state": "active",
                        "created_at": "2026-04-13T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "write-preview",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            record = FilesystemRecordStore(state_dir=state_dir).read_preview_record(
                "preview-opw-tenant-opw-pr-123"
            )
            self.assertEqual(record.canonical_url, "https://pr-123.ver-preview.shinycomputers.com")
            self.assertEqual(record.state, "active")

    def test_launchplane_previews_write_generation_assigns_next_sequence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record(preview_id="hpr_01jabc"))
            store.write_preview_generation_record(
                _generation_record(
                    "hpr_01jabc-generation-0001",
                    preview_id="hpr_01jabc",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            input_file = control_plane_root / "generation-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "state": "building",
                        "requested_reason": "manifest_changed",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "resolved_manifest_fingerprint": "launchplane-manifest-002",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "write-generation",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            record = store.read_preview_generation_record("hpr_01jabc-generation-0002")
            self.assertEqual(record.sequence, 2)
            self.assertEqual(record.state, "building")
            self.assertEqual(record.anchor_summary.head_sha, "aaaa2222")

    def test_launchplane_previews_write_generation_fails_when_preview_missing(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "generation-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "state": "building",
                        "requested_reason": "initial_create",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "resolved_manifest_fingerprint": "launchplane-manifest-001",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "write-generation",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No Launchplane preview found", result.output)

    def test_launchplane_previews_request_generation_updates_preview_and_generation_together(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            _write_runtime_environments_file(
                control_plane_root,
                """
schema_version = 1

[shared_env]
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip(),
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jabc",
                    state="active",
                    active_generation_id="hgen_01jabc_1",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_1",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    preview_id="hpr_01jabc",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            preview_input_file = control_plane_root / "preview-request.json"
            preview_input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "updated_at": "2026-04-13T12:20:00Z",
                    }
                ),
                encoding="utf-8",
            )
            generation_input_file = control_plane_root / "generation-request.json"
            generation_input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "state": "building",
                        "requested_reason": "manifest_changed",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "resolved_manifest_fingerprint": "launchplane-manifest-002",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "request-generation",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--preview-input-file",
                        str(preview_input_file),
                        "--generation-input-file",
                        str(generation_input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            preview = store.read_preview_record("hpr_01jabc")
            generation = store.read_preview_generation_record("hpr_01jabc-generation-0002")
            self.assertEqual(preview.active_generation_id, "hpr_01jabc-generation-0002")
            self.assertEqual(preview.latest_generation_id, "hpr_01jabc-generation-0002")
            self.assertEqual(preview.serving_generation_id, "hgen_01jabc_1")
            self.assertEqual(generation.sequence, 2)

    def test_launchplane_previews_write_from_generation_accepts_external_preview_evidence(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            preview_input_file = control_plane_root / "external-preview-request.json"
            preview_input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "created_at": "2026-04-13T12:00:00Z",
                        "updated_at": "2026-04-13T12:25:00Z",
                    }
                ),
                encoding="utf-8",
            )
            generation_input_file = control_plane_root / "external-generation-request.json"
            generation_input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "state": "ready",
                        "requested_reason": "external_preview_refresh",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "ready_at": "2026-04-13T12:25:00Z",
                        "finished_at": "2026-04-13T12:25:00Z",
                        "resolved_manifest_fingerprint": "verireel-preview-manifest-001",
                        "artifact_id": "artifact-verireel-pr-123",
                        "deploy_status": "pass",
                        "verify_status": "pass",
                        "overall_health_status": "pass",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "write-from-generation",
                        "--local-rehearsal",
                        "--state-dir",
                        str(state_dir),
                        "--preview-input-file",
                        str(preview_input_file),
                        "--generation-input-file",
                        str(generation_input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            preview = store.read_preview_record("preview-opw-tenant-opw-pr-123")
            generation = store.read_preview_generation_record(
                "preview-opw-tenant-opw-pr-123-generation-0001"
            )
            self.assertEqual(preview.canonical_url, "https://pr-123.ver-preview.shinycomputers.com")
            self.assertEqual(preview.state, "active")
            self.assertEqual(
                preview.active_generation_id,
                "preview-opw-tenant-opw-pr-123-generation-0001",
            )
            self.assertEqual(
                preview.serving_generation_id,
                "preview-opw-tenant-opw-pr-123-generation-0001",
            )
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.sequence, 1)
            self.assertEqual(generation.artifact_id, "artifact-verireel-pr-123")

    def test_launchplane_previews_mark_generation_ready_cuts_over_serving_generation(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jabc",
                    state="active",
                    active_generation_id="hpr_01jabc-generation-0002",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hpr_01jabc-generation-0002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    preview_id="hpr_01jabc",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hpr_01jabc-generation-0002",
                    preview_id="hpr_01jabc",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    ready_at="",
                )
            )
            input_file = control_plane_root / "generation-ready-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "generation_id": "hpr_01jabc-generation-0002",
                        "state": "ready",
                        "requested_reason": "manifest_changed",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "ready_at": "2026-04-13T12:25:00Z",
                        "resolved_manifest_fingerprint": "launchplane-manifest-002",
                        "artifact_id": "artifact-opw-124",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "mark-generation-ready",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            preview = store.read_preview_record("hpr_01jabc")
            self.assertEqual(preview.state, "active")
            self.assertEqual(preview.serving_generation_id, "hpr_01jabc-generation-0002")
            self.assertEqual(preview.active_generation_id, "hpr_01jabc-generation-0002")

    def test_launchplane_previews_mark_generation_failed_keeps_existing_serving_generation(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jabc",
                    state="active",
                    active_generation_id="hpr_01jabc-generation-0002",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hpr_01jabc-generation-0002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    preview_id="hpr_01jabc",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hpr_01jabc-generation-0002",
                    preview_id="hpr_01jabc",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    ready_at="",
                )
            )
            input_file = control_plane_root / "generation-failed-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "anchor_head_sha": "aaaa2222",
                        "generation_id": "hpr_01jabc-generation-0002",
                        "state": "failed",
                        "requested_reason": "manifest_changed",
                        "requested_at": "2026-04-13T12:20:00Z",
                        "failed_at": "2026-04-13T12:24:00Z",
                        "resolved_manifest_fingerprint": "launchplane-manifest-002",
                        "artifact_id": "artifact-opw-124",
                        "failure_stage": "deploying",
                        "failure_summary": "Replacement generation failed during deploy.",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "mark-generation-failed",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            preview = store.read_preview_record("hpr_01jabc")
            self.assertEqual(preview.state, "failed")
            self.assertEqual(preview.serving_generation_id, "hgen_01jabc_1")
            self.assertEqual(preview.latest_generation_id, "hpr_01jabc-generation-0002")

    def test_launchplane_previews_destroy_preview_clears_runtime_links(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jabc",
                    state="teardown_pending",
                    active_generation_id="hpr_01jabc-generation-0002",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hpr_01jabc-generation-0002",
                )
            )
            input_file = control_plane_root / "destroy-preview-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "opw",
                        "anchor_repo": "tenant-opw",
                        "anchor_pr_number": 123,
                        "destroyed_at": "2026-04-14T12:14:00Z",
                        "destroy_reason": "merged_after_grace_window",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "destroy-preview",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            preview = store.read_preview_record("hpr_01jabc")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.active_generation_id, "")
            self.assertEqual(preview.serving_generation_id, "")
            self.assertEqual(preview.latest_generation_id, "hpr_01jabc-generation-0002")

    def test_launchplane_previews_write_destroyed_ingests_external_cleanup_evidence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    state="active",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    active_generation_id="preview-verireel-pr-123-generation-0003",
                    serving_generation_id="preview-verireel-pr-123-generation-0003",
                    latest_generation_id="preview-verireel-pr-123-generation-0003",
                )
            )
            input_file = control_plane_root / "write-destroyed-request.json"
            input_file.write_text(
                json.dumps(
                    {
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 123,
                        "destroyed_at": "2026-04-16T08:12:00Z",
                        "destroy_reason": "external_preview_cleanup_completed",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "write-destroyed",
                    "--local-rehearsal",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["preview_id"], "preview-verireel-pr-123")
            self.assertEqual(payload["transition"], "destroyed")

            preview = store.read_preview_record("preview-verireel-pr-123")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroyed_at, "2026-04-16T08:12:00Z")
            self.assertEqual(preview.destroy_reason, "external_preview_cleanup_completed")
            self.assertEqual(preview.active_generation_id, "")
            self.assertEqual(preview.serving_generation_id, "")
            self.assertEqual(
                preview.latest_generation_id, "preview-verireel-pr-123-generation-0003"
            )

    def test_launchplane_previews_show_tenant_uses_companion_sha_snapshot_for_enablement_recipe(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=129,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/129",
                    anchor_head_sha="dddd4444",
                    action="labeled",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                    request_metadata_companions=(
                        LaunchplaneCompanionPullRequestReference(
                            repo="shared-addons", pr_number=456
                        ),
                    ),
                    request_metadata_companion_summaries=(
                        PreviewPullRequestSummary(
                            repo="shared-addons",
                            pr_number=456,
                            head_sha="bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
                            pr_url="https://github.com/every/shared-addons/pull/456",
                        ),
                    ),
                )
            )

            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch(
                    "control_plane.workflows.launchplane.fetch_github_pull_request_head",
                    side_effect=AssertionError("unexpected live companion lookup"),
                ),
            ):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "show-tenant",
                        "--state-dir",
                        str(state_dir),
                        "--context",
                        "opw",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            enablement_by_pr = {
                item["anchor_pr_number"]: item for item in payload["preview_enablement"]
            }
            action = enablement_by_pr[129]["action"]
            self.assertEqual(action["status"], "actionable")
            self.assertIn("bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222", action["recipe"])
            self.assertIn("companion_summaries", action["recipe"])
            self.assertEqual(
                enablement_by_pr[129]["request_metadata_companion_summaries"][0]["repo"],
                "shared-addons",
            )

    def test_launchplane_previews_show_tenant_blocks_unresolved_companion_enablement_recipe(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=130,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/130",
                    anchor_head_sha="eeee5555",
                    action="labeled",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                    request_metadata_companions=(
                        LaunchplaneCompanionPullRequestReference(
                            repo="shared-addons", pr_number=456
                        ),
                    ),
                )
            )

            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch(
                    "control_plane.workflows.launchplane.fetch_github_pull_request_head",
                    side_effect=AssertionError("unexpected live companion lookup"),
                ),
            ):
                result = runner.invoke(
                    CLI_MAIN,
                    [
                        "launchplane-previews",
                        "show-tenant",
                        "--state-dir",
                        str(state_dir),
                        "--context",
                        "opw",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            enablement_by_pr = {
                item["anchor_pr_number"]: item for item in payload["preview_enablement"]
            }
            action = enablement_by_pr[130]["action"]
            self.assertEqual(action["status"], "blocked")
            self.assertEqual(action["recipe"], "")
            self.assertIn("Companion PR snapshots", action["headline"])

    def test_launchplane_previews_list_keeps_destroyed_previews_visible_and_filters_by_context(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jabc",
                    updated_at="2026-04-13T12:14:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    preview_id="hpr_01jabc",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jxyz",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://launchplane.example/previews/opw/tenant-opw/pr-124",
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_01jxyz_1",
                    latest_manifest_fingerprint="launchplane-manifest-099",
                    updated_at="2026-04-13T12:18:00Z",
                    destroyed_at="2026-04-13T12:18:00Z",
                    destroy_reason="closed_without_merge",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jxyz_1",
                    preview_id="hpr_01jxyz",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-099",
                    artifact_id="artifact-opw-124",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jcm",
                    context="cm",
                    anchor_repo="tenant-cm",
                    anchor_pr_number=10,
                    anchor_pr_url="https://github.com/every/tenant-cm/pull/10",
                    preview_label="cm/tenant-cm/pr-10",
                    canonical_url="https://launchplane.example/previews/cm/tenant-cm/pr-10",
                    updated_at="2026-04-13T12:19:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jcm_1",
                    preview_id="hpr_01jcm",
                    anchor_repo="tenant-cm",
                    anchor_pr_number=10,
                    anchor_pr_url="https://github.com/every/tenant-cm/pull/10",
                    anchor_head_sha="cccc3333",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-cm-001",
                    artifact_id="artifact-cm-010",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "list",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                [row["preview_id"] for row in payload["previews"]],
                ["hpr_01jxyz", "hpr_01jabc"],
            )
            self.assertEqual(payload["previews"][0]["state"], "destroyed")
            self.assertEqual(payload["previews"][0]["artifact_id"], "artifact-opw-124")
            self.assertIn("destroyed", payload["previews"][0]["status_summary"].lower())

    def test_launchplane_previews_history_marks_latest_and_serving_generations(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="failed",
                    active_generation_id="hgen_01jabc_2",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_2",
                    latest_manifest_fingerprint="launchplane-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="launchplane-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="launchplane-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="fail",
                    verify_status="skipped",
                    overall_health_status="fail",
                    failure_stage="deploying",
                    failure_summary="Replacement generation failed during deploy.",
                    ready_at="",
                    failed_at="2026-04-13T12:15:00Z",
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "launchplane-previews",
                    "history",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--anchor-repo",
                    "tenant-opw",
                    "--pr-number",
                    "123",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["generation_count"], 2)
            self.assertEqual(
                [item["generation_id"] for item in payload["generations"]],
                ["hgen_01jabc_2", "hgen_01jabc_1"],
            )
            self.assertTrue(payload["generations"][0]["is_latest"])
            self.assertTrue(payload["generations"][0]["is_active"])
            self.assertFalse(payload["generations"][0]["is_serving"])
            self.assertFalse(payload["generations"][1]["is_latest"])
            self.assertFalse(payload["generations"][1]["is_active"])
            self.assertTrue(payload["generations"][1]["is_serving"])


if __name__ == "__main__":
    unittest.main()
