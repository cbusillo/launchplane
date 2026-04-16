import hashlib
import hmac
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.harbor import (
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    apply_preview_destroyed_transition,
    build_preview_canonical_url,
    build_preview_generation_record,
    build_preview_label,
    build_preview_record,
    build_preview_route_path,
    classify_pull_request_event_for_harbor,
    harbor_anchor_repo_context,
    harbor_anchor_repo_eligible,
    generate_preview_generation_id,
    generate_preview_id,
    harbor_preview_label_enabled,
    parse_preview_request_metadata,
    resolve_harbor_preview_base_url,
)


def _preview_record(
    *,
    preview_id: str = "hpr_01jabc",
    context: str = "opw",
    anchor_repo: str = "tenant-opw",
    anchor_pr_number: int = 123,
    anchor_pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    preview_label: str = "opw/tenant-opw/pr-123",
    canonical_url: str = "https://harbor.example/previews/opw/tenant-opw/pr-123",
    state: str = "active",
    active_generation_id: str = "hgen_01jabc_1",
    serving_generation_id: str = "hgen_01jabc_1",
    latest_generation_id: str = "hgen_01jabc_1",
    latest_manifest_fingerprint: str = "harbor-manifest-001",
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


def _generation_record(
    generation_id: str,
    *,
    preview_id: str = "hpr_01jabc",
    anchor_repo: str = "tenant-opw",
    anchor_pr_number: int = 123,
    anchor_pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    anchor_head_sha: str = "aaaa1111",
    sequence: int,
    state: str,
    manifest_fingerprint: str,
    artifact_id: str,
    deploy_status: str = "pass",
    verify_status: str = "pass",
    overall_health_status: str = "pass",
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
    action: str = "opened",
    pr_state: str = "open",
    updated_at: str = "2026-04-14T11:15:00Z",
    label_enabled: bool = False,
    action_label: str = "",
    request_metadata_status: str = "missing",
    request_metadata_error: str = "",
    request_metadata_baseline_channel: str = "",
    request_metadata_companions: tuple[dict[str, object], ...] = (),
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
        label_enabled=label_enabled,
        action_label=action_label,
        request_metadata_status=request_metadata_status,
        request_metadata_error=request_metadata_error,
        request_metadata_baseline_channel=request_metadata_baseline_channel,
        request_metadata_companions=request_metadata_companions,
    )


def _backup_gate_record(
    *,
    record_id: str = "backup-opw-prod-20260414T111500Z",
    context: str = "opw",
    instance: str = "prod",
    created_at: str = "2026-04-14T11:15:00Z",
    source: str = "prod-gate",
    required: bool = True,
    status: str = "pass",
    evidence: dict[str, str] | None = None,
) -> BackupGateRecord:
    resolved_evidence = evidence if evidence is not None else {"snapshot": "s3://harbor/opw/prod/2026-04-14"}
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
    deploy_status: str = "pass",
    destination_health_status: str = "pass",
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
        post_deploy_update=PostDeployUpdateEvidence(attempted=True, status="pass", detail="Updated"),
        destination_health=HealthcheckEvidence(status=destination_health_status),
    )


def _write_release_tuples_file(control_plane_root: Path) -> None:
    release_tuples_file = control_plane_root / "config" / "release-tuples.toml"
    release_tuples_file.parent.mkdir(parents=True, exist_ok=True)
    release_tuples_file.write_text(
        """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "opw-testing-2026-04-13"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "1111111111111111111111111111111111111111"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[contexts.cm.channels.testing]
tuple_id = "cm-testing-2026-04-13"

[contexts.cm.channels.testing.repo_shas]
tenant-cm = "3333333333333333333333333333333333333333"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _write_runtime_environments_file(control_plane_root: Path) -> None:
    environments_file = control_plane_root / "config" / "runtime-environments.toml"
    environments_file.parent.mkdir(parents=True, exist_ok=True)
    environments_file.write_text(
        """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"
GITHUB_WEBHOOK_SECRET = "harbor-webhook-secret"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.cm.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _github_pull_request_webhook_payload(
    *,
    action: str = "labeled",
    repo: str = "tenant-opw",
    pr_number: int = 123,
    pr_url: str = "https://github.com/every/tenant-opw/pull/123",
    body: str = (
        "```harbor-preview\n"
        "schema_version = 1\n"
        'baseline_channel = "testing"\n'
        "```\n"
    ),
    state: str = "open",
    merged: bool = False,
    head_sha: str = "aaaa1111",
    labels: list[dict[str, str]] | None = None,
    action_label: str = "harbor-preview",
    created_at: str = "2026-04-13T12:00:00Z",
    updated_at: str = "2026-04-13T12:15:00Z",
    closed_at: str = "2026-04-13T12:17:00Z",
) -> dict[str, object]:
    resolved_labels = labels if labels is not None else [{"name": "harbor-preview"}]
    payload: dict[str, object] = {
        "action": action,
        "number": pr_number,
        "repository": {"name": repo},
        "pull_request": {
            "html_url": pr_url,
            "body": body,
            "state": state,
            "merged": merged,
            "head": {"sha": head_sha},
            "labels": resolved_labels,
            "created_at": created_at,
            "updated_at": updated_at,
            "closed_at": closed_at,
            "merged_at": closed_at if merged else None,
        },
    }
    if action in {"labeled", "unlabeled"}:
        payload["label"] = {"name": action_label}
    return payload


def _github_webhook_signature(payload: dict[str, object], secret: str = "harbor-webhook-secret") -> str:
    payload_bytes = json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _github_webhook_replay_envelope(
    *,
    payload: dict[str, object] | None = None,
    payload_text: str = "",
    signature_256: str = "",
    allow_unsigned: bool = False,
    event_name: str = "pull_request",
    delivery_id: str = "",
    delivery_source: str = "replay-envelope",
    capture: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapter": "github_webhook",
        "event_name": event_name,
        "signature_256": signature_256,
        "allow_unsigned": allow_unsigned,
        "delivery_id": delivery_id,
        "delivery_source": delivery_source,
        "payload_text": payload_text,
        "payload": payload,
        "capture": capture,
    }


class HarborPreviewReadModelTests(unittest.TestCase):
    def test_harbor_preview_identity_helpers_are_deterministic(self) -> None:
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
                preview_base_url="https://harbor.example",
                context_name="opw",
                anchor_repo="tenant-opw",
                anchor_pr_number=123,
            ),
            "https://harbor.example/previews/opw/tenant-opw/pr-123",
        )

    def test_build_preview_record_reuses_stable_identity_for_same_anchor(self) -> None:
        first_record = build_preview_record(
            context_name="opw",
            anchor_repo="tenant-opw",
            anchor_pr_number=123,
            anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
            created_at="2026-04-13T12:00:00Z",
            updated_at="2026-04-13T12:10:00Z",
            preview_base_url="https://harbor.example",
            state="active",
        )
        reopened_record = build_preview_record(
            context_name="opw",
            anchor_repo="tenant-opw",
            anchor_pr_number=123,
            anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
            created_at="2026-04-14T09:00:00Z",
            updated_at="2026-04-14T09:05:00Z",
            preview_base_url="https://harbor.example",
            state="pending",
        )

        self.assertEqual(first_record.preview_id, reopened_record.preview_id)
        self.assertEqual(first_record.preview_label, reopened_record.preview_label)
        self.assertEqual(first_record.canonical_url, reopened_record.canonical_url)

    def test_resolve_harbor_preview_base_url_reads_context_runtime_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            resolved_base_url = resolve_harbor_preview_base_url(
                control_plane_root=control_plane_root,
                context_name="opw",
            )

        self.assertEqual(resolved_base_url, "https://harbor.example")

    def test_resolve_harbor_preview_base_url_fails_closed_when_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Exception, "HARBOR_PREVIEW_BASE_URL"):
                resolve_harbor_preview_base_url(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                )

    def test_build_preview_generation_record_links_anchor_and_sequence(self) -> None:
        generation_record = build_preview_generation_record(
            preview_id="preview-opw-tenant-opw-pr-123",
            sequence=2,
            state="failed",
            requested_reason="manifest_changed",
            requested_at="2026-04-13T12:10:00Z",
            resolved_manifest_fingerprint="harbor-manifest-002",
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
        )

        self.assertEqual(
            generation_record.generation_id,
            "preview-opw-tenant-opw-pr-123-generation-0002",
        )
        self.assertEqual(generation_record.anchor_summary.repo, "tenant-opw")
        self.assertEqual(generation_record.anchor_summary.pr_number, 123)
        self.assertEqual(generation_record.failure_stage, "deploying")

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
            manifest_fingerprint="harbor-manifest-002",
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
            manifest_fingerprint="harbor-manifest-002",
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
            manifest_fingerprint="harbor-manifest-002",
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

    def test_apply_preview_destroyed_transition_clears_runtime_links_and_keeps_evidence(self) -> None:
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

    def test_harbor_preview_label_enabled_matches_configured_label(self) -> None:
        self.assertTrue(harbor_preview_label_enabled(label_names=("bug", "harbor-preview")))
        self.assertFalse(harbor_preview_label_enabled(label_names=("bug", "needs-review")))

    def test_harbor_anchor_repo_resolution_accepts_tenant_repos_only(self) -> None:
        self.assertEqual(harbor_anchor_repo_context(repo="tenant-opw"), "opw")
        self.assertEqual(harbor_anchor_repo_context(repo="tenant-cm"), "cm")
        self.assertTrue(harbor_anchor_repo_eligible(repo="tenant-opw"))
        self.assertTrue(harbor_anchor_repo_eligible(repo="tenant-cm"))
        self.assertFalse(harbor_anchor_repo_eligible(repo="shared-addons"))
        self.assertFalse(harbor_anchor_repo_eligible(repo="control-plane"))

    def test_classify_pull_request_event_for_harbor_enables_preview_when_label_added(self) -> None:
        event = GitHubPullRequestEvent(
            action="labeled",
            repo="tenant-opw",
            pr_number=123,
            pr_url="https://github.com/every/tenant-opw/pull/123",
            state="open",
            head_sha="aaaa1111",
            label_names=("harbor-preview",),
            action_label="harbor-preview",
        )

        action = classify_pull_request_event_for_harbor(event=event, preview=None)

        self.assertEqual(action, "enable_preview")

    def test_classify_pull_request_event_for_harbor_refreshes_enabled_preview_on_sync(self) -> None:
        event = GitHubPullRequestEvent(
            action="synchronize",
            repo="tenant-opw",
            pr_number=123,
            pr_url="https://github.com/every/tenant-opw/pull/123",
            state="open",
            head_sha="bbbb2222",
            label_names=("harbor-preview",),
        )

        action = classify_pull_request_event_for_harbor(
            event=event,
            preview=_preview_record(state="active"),
        )

        self.assertEqual(action, "refresh_preview")

    def test_classify_pull_request_event_for_harbor_destroys_preview_on_close(self) -> None:
        event = GitHubPullRequestEvent(
            action="closed",
            repo="tenant-opw",
            pr_number=123,
            pr_url="https://github.com/every/tenant-opw/pull/123",
            state="closed",
            merged=False,
            head_sha="bbbb2222",
            label_names=("harbor-preview",),
        )

        action = classify_pull_request_event_for_harbor(
            event=event,
            preview=_preview_record(state="active"),
        )

        self.assertEqual(action, "destroy_preview")

    def test_classify_pull_request_event_for_harbor_reenables_destroyed_preview_on_reopen(self) -> None:
        event = GitHubPullRequestEvent(
            action="reopened",
            repo="tenant-opw",
            pr_number=123,
            pr_url="https://github.com/every/tenant-opw/pull/123",
            state="open",
            head_sha="cccc3333",
            label_names=("harbor-preview",),
        )

        action = classify_pull_request_event_for_harbor(
            event=event,
            preview=_preview_record(state="destroyed"),
        )

        self.assertEqual(action, "enable_preview")

    def test_classify_pull_request_event_for_harbor_ignores_unlabeled_open_pr(self) -> None:
        event = GitHubPullRequestEvent(
            action="opened",
            repo="tenant-opw",
            pr_number=123,
            pr_url="https://github.com/every/tenant-opw/pull/123",
            state="open",
            head_sha="cccc3333",
            label_names=("bug",),
        )

        action = classify_pull_request_event_for_harbor(event=event, preview=None)

        self.assertEqual(action, "ignore")

    def test_parse_preview_request_metadata_reads_harbor_fenced_block(self) -> None:
        result = parse_preview_request_metadata(
            pr_body=(
                "Some intro text\n\n"
                "```harbor-preview\n"
                "schema_version = 1\n"
                "\n"
                "[[companions]]\n"
                'repo = "shared-addons"\n'
                "pr_number = 456\n"
                "```\n"
            )
        )

        self.assertEqual(result.status, "valid")
        self.assertIsNotNone(result.metadata)
        assert result.metadata is not None
        self.assertEqual(result.metadata.baseline_channel, "testing")
        self.assertEqual(result.metadata.companions[0].repo, "shared-addons")
        self.assertEqual(result.metadata.companions[0].pr_number, 456)

    def test_parse_preview_request_metadata_is_missing_without_harbor_block(self) -> None:
        result = parse_preview_request_metadata(pr_body="Regular PR body without Harbor metadata.")

        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.metadata)

    def test_parse_preview_request_metadata_fails_closed_for_invalid_companion_repo(self) -> None:
        result = parse_preview_request_metadata(
            pr_body=(
                "```harbor-preview\n"
                "schema_version = 1\n"
                "\n"
                "[[companions]]\n"
                'repo = "tenant-cm"\n'
                "pr_number = 456\n"
                "```\n"
            )
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("not allowlisted", result.error)

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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="harbor-manifest-002",
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
            self.assertEqual([record.generation_id for record in generations], [
                "hgen_01jabc_2",
                "hgen_01jabc_1",
            ])

    def test_harbor_previews_show_active_preview(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_show_surfaces_first_page_summary_fields(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "https://harbor.example/previews/opw/tenant-opw/pr-123",
            )
            self.assertEqual(payload["preview"]["preview_label"], "opw/tenant-opw/pr-123")
            self.assertEqual(payload["trust_summary"]["artifact_id"], "artifact-opw-123")
            self.assertEqual(
                payload["trust_summary"]["manifest_fingerprint"],
                "harbor-manifest-001",
            )
            self.assertEqual(
                payload["lifecycle_summary"]["next_action"],
                "Harbor will keep this preview until the current destroy-after deadline or a lifecycle event replaces it.",
            )
            self.assertEqual(
                payload["input_summary"]["source_map"][0]["repo"],
                "tenant-opw",
            )

    def test_harbor_previews_render_status_page_writes_html_summary(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Harbor control plane", rendered_html)
            self.assertIn("Preview detail", rendered_html)
            self.assertIn('class="preview-detail-mast"', rendered_html)
            self.assertIn("Current preview evidence", rendered_html)
            self.assertIn('class="preview-detail-grid"', rendered_html)
            self.assertIn("tenant-opw PR 123", rendered_html)
            self.assertIn("opw/tenant-opw/pr-123", rendered_html)
            self.assertIn("https://harbor.example/previews/opw/tenant-opw/pr-123", rendered_html)
            self.assertIn("artifact-opw-123", rendered_html)
            self.assertIn("harbor-manifest-001", rendered_html)
            self.assertIn("Serving the latest requested generation.", rendered_html)
            self.assertIn("Write-side Harbor recipes", rendered_html)
            self.assertIn("request-generation", rendered_html)
            self.assertIn("destroy-preview", rendered_html)
            self.assertIn('id="operator-actions"', rendered_html)
            self.assertIn(
                "This preview is live at the stable Harbor route and serving the latest requested generation.",
                rendered_html,
            )
            self.assertIn("Raw payload JSON", rendered_html)
            self.assertIn("Open preview URL", rendered_html)
            self.assertIn("serving / latest", rendered_html)

    def test_harbor_previews_show_tenant_surfaces_environment_and_preview_lanes(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
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
                    manifest_fingerprint="harbor-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_pending",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-124",
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
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
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
                    preview_id="hpr_harbor",
                    anchor_pr_number=126,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/126",
                    preview_label="opw/tenant-opw/pr-126",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-126",
                    active_generation_id="hgen_harbor_1",
                    serving_generation_id="hgen_harbor_1",
                    latest_generation_id="hgen_harbor_1",
                    updated_at="2026-04-14T11:18:00Z",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_harbor_1",
                    preview_id="hpr_harbor",
                    anchor_pr_number=126,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/126",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="harbor-manifest-harbor",
                    artifact_id="artifact-harbor",
                ).model_copy(update={"requested_reason": "operator_requested_refresh"})
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertEqual(payload["environments"]["testing"]["live"]["artifact_id"], "artifact-testing")
            self.assertEqual(payload["environments"]["prod"]["live"]["artifact_id"], "artifact-prod")
            self.assertEqual(payload["promotion_summary"]["status"], "candidate")
            self.assertEqual(payload["promotion_action"]["status"], "blocked")
            self.assertEqual(payload["environment_actions"]["testing"]["status"], "actionable")
            self.assertEqual(payload["environment_actions"]["prod"]["status"], "actionable")
            self.assertIn("ship resolve", payload["environment_actions"]["testing"]["recipe"])
            self.assertEqual(payload["promotion_action"]["candidate_artifact_id"], "artifact-testing")
            self.assertEqual(payload["promotion_action"]["current_prod_artifact_id"], "artifact-prod")
            self.assertEqual(payload["promotion_action"]["evidence_checks"][3]["label"], "Prod backup gate")
            self.assertIn("no prod backup-gate evidence", payload["promotion_action"]["evidence_checks"][3]["detail"].lower())
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
            self.assertEqual(enablement_by_pr[124]["request_source"], "github_label")
            self.assertEqual(enablement_by_pr[124]["action"]["status"], "existing_preview")
            self.assertEqual(enablement_by_pr[126]["state"], "running")
            self.assertEqual(enablement_by_pr[126]["request_source"], "harbor")
            self.assertEqual(enablement_by_pr[126]["action"]["status"], "existing_preview")

    def test_harbor_previews_ingest_pr_event_persists_preview_enablement_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            input_file = Path(temporary_directory_name) / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "opened",
                        "repo": "tenant-opw",
                        "pr_number": 125,
                        "pr_url": "https://github.com/every/tenant-opw/pull/125",
                        "occurred_at": "2026-04-14T11:16:00Z",
                        "pr_body": "Regular PR body without Harbor metadata.",
                        "state": "open",
                        "merged": False,
                        "head_sha": "bbbb2222",
                        "label_names": [],
                        "action_label": "",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "ingest-pr-event",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertIsNotNone(payload["enablement_record"])
            self.assertFalse(payload["enablement_record"]["label_enabled"])
            store = FilesystemRecordStore(state_dir=state_dir)
            record = store.read_preview_enablement_record("opw-tenant-opw-pr-125")
            self.assertEqual(record.anchor_pr_number, 125)
            self.assertEqual(record.pr_state, "open")
            self.assertFalse(record.label_enabled)
            self.assertEqual(record.request_metadata_baseline_channel, "")
            self.assertEqual(record.request_metadata_companions, ())

    def test_harbor_previews_ingest_pr_event_persists_valid_preview_metadata_snapshot(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            input_file = Path(temporary_directory_name) / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "opened",
                        "repo": "tenant-opw",
                        "pr_number": 126,
                        "pr_url": "https://github.com/every/tenant-opw/pull/126",
                        "occurred_at": "2026-04-14T11:18:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            'baseline_channel = "testing"\n'
                            "[[companions]]\n"
                            'repo = "shared-addons"\n'
                            "pr_number = 456\n"
                            "```"
                        ),
                        "state": "open",
                        "merged": False,
                        "head_sha": "cccc3333",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "ingest-pr-event",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = store.read_preview_enablement_record("opw-tenant-opw-pr-126")
            self.assertEqual(record.request_metadata_status, "valid")
            self.assertEqual(record.request_metadata_baseline_channel, "testing")
            self.assertEqual(record.request_metadata_companions[0].repo, "shared-addons")
            self.assertEqual(record.request_metadata_companions[0].pr_number, 456)

    def test_harbor_previews_show_tenant_uses_valid_metadata_snapshot_for_enablement_actions(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=129,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/129",
                    anchor_head_sha="dddd4444",
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_site_release_tuples_file_resolves_enablement_recipe(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            state_dir = temporary_directory / "state"
            output_dir = temporary_directory / "site"
            release_tuples_file = temporary_directory / "release-tuples.toml"
            release_tuples_file.write_text(
                """
schema_version = 1

[contexts.opw.channels.testing]
tuple_id = "opw-testing-2026-04-13"

[contexts.opw.channels.testing.repo_shas]
tenant-opw = "1111111111111111111111111111111111111111"
shared-addons = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=130,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/130",
                    anchor_head_sha="dddd4444",
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "render-site",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--release-tuples-file",
                    str(release_tuples_file),
                    "--output-dir",
                    str(output_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("opw-testing-2026-04-13", index_html)
            self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", index_html)
            self.assertNotIn("&lt;resolved-baseline-tuple-id&gt;", index_html)
            self.assertNotIn("&lt;resolved-manifest-fingerprint&gt;", index_html)

    def test_harbor_previews_render_index_page_leads_with_tenant_environment_when_scoped(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index.html"
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
                    manifest_fingerprint="harbor-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
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
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Testing is carrying a newer artifact than prod and is the current promotion candidate.", rendered_html)
            self.assertIn("Testing is ready to promote into prod.", rendered_html)
            self.assertIn("Rebuild long-lived lanes", rendered_html)
            self.assertIn("Re-ship current testing artifact", rendered_html)
            self.assertIn("ship resolve -&gt; ship execute", rendered_html)
            self.assertIn("Promotion candidate", rendered_html)
            self.assertIn("Latest prod backup gate backup-opw-prod-20260414T111500Z passed and can authorize promotion.", rendered_html)
            self.assertIn("promote resolve", rendered_html)
            self.assertIn("promote execute", rendered_html)
            self.assertNotIn("backup-gates write", rendered_html)
            self.assertIn("Why each PR does or does not have a preview", rendered_html)
            self.assertIn("Eligible tenant PR. No preview request is active yet.", rendered_html)
            self.assertIn("GitHub label harbor-preview requested a preview, but Harbor has not created the preview record yet.", rendered_html)
            self.assertIn("Request Harbor preview", rendered_html)
            self.assertIn("Show Harbor request recipe", rendered_html)
            self.assertIn("request-generation", rendered_html)
            self.assertIn("artifact-testing", rendered_html)
            self.assertIn("artifact-prod", rendered_html)
            self.assertIn("Pull request previews", rendered_html)

    def test_harbor_previews_render_index_page_surfaces_backup_gate_recipe_when_promotion_blocked(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index.html"
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
                main,
                [
                    "harbor-previews",
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
            self.assertIn("A newer testing artifact exists, but Harbor cannot promote it yet.", rendered_html)
            self.assertIn("backup-gates write", rendered_html)
            self.assertNotIn("promote resolve", rendered_html)

    def test_harbor_previews_render_index_page_leads_with_enablement_when_no_lane_evidence_exists(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_enablement_record(
                _preview_enablement_record(
                    anchor_pr_number=130,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/130",
                    anchor_head_sha="eeee5555",
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
                    request_metadata_status="valid",
                    request_metadata_baseline_channel="testing",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Preview enablement is the first meaningful control surface", rendered_html)
            self.assertIn("Why each PR does or does not have a preview", rendered_html)
            self.assertIn("Materialize requested Harbor preview", rendered_html)
            self.assertNotIn("Rebuild long-lived lanes", rendered_html)
            self.assertNotIn("Harbor cannot plan the next promotion yet.", rendered_html)

    def test_harbor_previews_render_index_page_marks_missing_lane_action_evidence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index.html"
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
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_show_tenant_marks_in_sync_promotion_state(self) -> None:
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
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_index_page_writes_preview_dashboard(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_live",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-123",
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
                    manifest_fingerprint="harbor-manifest-live",
                    artifact_id="artifact-live",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_fail",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-124",
                    state="failed",
                    active_generation_id="hgen_fail_2",
                    serving_generation_id="hgen_fail_1",
                    latest_generation_id="hgen_fail_2",
                    latest_manifest_fingerprint="harbor-manifest-fail",
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
                    manifest_fingerprint="harbor-manifest-prev",
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
                    manifest_fingerprint="harbor-manifest-fail",
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
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-125",
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
                    manifest_fingerprint="harbor-manifest-dead",
                    artifact_id="artifact-dead",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Harbor control plane", rendered_html)
            self.assertIn("Pull request previews", rendered_html)
            self.assertIn("Fleet focus", rendered_html)
            self.assertIn("Reviewable now", rendered_html)
            self.assertIn("Needs attention", rendered_html)
            self.assertIn("Live review", rendered_html)
            self.assertIn("Retained evidence", rendered_html)
            self.assertIn("Policy snapshot", rendered_html)
            self.assertIn('data-filter-control="attention"', rendered_html)
            self.assertIn('data-preview-row', rendered_html)
            self.assertIn("Serving older generation", rendered_html)
            self.assertIn("Evidence only", rendered_html)
            self.assertIn("opw/tenant-opw/pr-123", rendered_html)
            self.assertIn("opw/tenant-opw/pr-124", rendered_html)
            self.assertIn("opw/tenant-opw/pr-125", rendered_html)

    def test_harbor_previews_render_index_page_surfaces_scope_controls_for_multi_context_inventory(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-index-all.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_opw",
                    context="opw",
                    anchor_repo="tenant-opw",
                    anchor_pr_number=123,
                    preview_label="opw/tenant-opw/pr-123",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-123",
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
                    manifest_fingerprint="harbor-manifest-opw",
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
                    canonical_url="https://harbor.example/previews/cm/tenant-cm/pr-88",
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
                    manifest_fingerprint="harbor-manifest-cm",
                    artifact_id="artifact-cm",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_policy_page_writes_contract_summary(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-policy.html"
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
                    manifest_fingerprint="harbor-manifest-live",
                    artifact_id="artifact-live",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Harbor control plane", rendered_html)
            self.assertIn("How Harbor decides what becomes a preview", rendered_html)
            self.assertIn("harbor-preview", rendered_html)
            self.assertIn("shared-addons", rendered_html)
            self.assertIn("tenant-opw", rendered_html)
            self.assertIn("tenant-cm", rendered_html)
            self.assertIn("testing", rendered_html)

    def test_harbor_previews_render_policy_page_shows_context_distribution_for_multi_context_inventory(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-policy-all.html"
            store = FilesystemRecordStore(state_dir=state_dir)
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
                    manifest_fingerprint="harbor-manifest-opw",
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
                    manifest_fingerprint="harbor-manifest-cm",
                    artifact_id="artifact-cm",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_site_writes_linked_bundle(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-live",
                    artifact_id="artifact-live",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
            self.assertIn('#operator-actions', index_html)
            self.assertIn('href="policy.html"', index_html)
            self.assertIn('href="../../../index.html"', detail_html)
            self.assertIn('href="../../../policy.html"', detail_html)
            self.assertIn('href="previews/opw/tenant-opw/pr-123.html"', policy_html)

    def test_harbor_previews_render_site_enablement_row_links_to_existing_preview_detail(self) -> None:
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
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-124",
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
                    label_enabled=True,
                    action="labeled",
                    action_label="harbor-preview",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_site_writes_environment_detail_pages_and_links_from_overview(self) -> None:
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
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_render_site_environment_detail_marks_partial_evidence_cleanly(self) -> None:
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
                main,
                [
                    "harbor-previews",
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
            self.assertIn("No live promotion record is attached to this lane inventory.", testing_detail_html)
            self.assertIn("No authorized backup gate is attached to this lane yet.", testing_detail_html)
            self.assertIn("No deployment history recorded for this lane yet.", testing_detail_html)
            self.assertIn("No promotion history recorded into this lane yet.", testing_detail_html)

    def test_harbor_previews_render_site_writes_promotion_detail_page_and_link_from_overview(self) -> None:
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
                main,
                [
                    "harbor-previews",
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
            self.assertIn("promotion-2026-04-14T11:10:00Z-opw-testing-to-prod", promotion_detail_html)

    def test_harbor_previews_render_status_page_calls_out_failed_latest_replacement(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                _preview_record(
                    state="failed",
                    active_generation_id="hgen_01jabc_2",
                    serving_generation_id="hgen_01jabc_1",
                    latest_generation_id="hgen_01jabc_2",
                    latest_manifest_fingerprint="harbor-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="harbor-manifest-002",
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
                main,
                [
                    "harbor-previews",
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
            self.assertIn("Latest replacement failed. Harbor is still serving the older preview.", rendered_html)
            self.assertIn("Replacement generation failed during deploy.", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertIn("hgen_01jabc_2", rendered_html)

    def test_harbor_previews_render_status_page_preserves_destroyed_preview_evidence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "This preview has already been destroyed. Harbor is retaining the record as evidence.",
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
                "Latest replacement failed. Harbor is still serving the older preview.",
                rendered_html,
            )

    def test_harbor_previews_render_status_page_calls_out_paused_preview_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "This preview is intentionally paused. Harbor is holding the current review evidence in place.",
                rendered_html,
            )
            self.assertIn("2026-04-14T16:20:00Z", rendered_html)
            self.assertIn("Blocked until Harbor resumes the preview.", rendered_html)
            self.assertIn("Open preview URL", rendered_html)
            self.assertNotIn(
                "This preview has already been destroyed. Harbor is retaining the record as evidence.",
                rendered_html,
            )

    def test_harbor_previews_render_status_page_calls_out_teardown_pending_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "This preview is queued for teardown. Harbor is keeping the current runtime available until cleanup completes.",
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
                "This preview is intentionally paused. Harbor is holding the current review evidence in place.",
                rendered_html,
            )

    def test_harbor_previews_render_status_page_calls_out_in_progress_replacement(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="harbor-manifest-002",
                    artifact_id="artifact-opw-124",
                    deploy_status="pending",
                    verify_status="pending",
                    overall_health_status="pending",
                    ready_at="",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "A replacement generation is in progress. Harbor is still serving the current preview.",
                rendered_html,
            )
            self.assertIn("Current stage", rendered_html)
            self.assertIn("deploying", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertIn("2026-04-13T12:10:00Z", rendered_html)
            self.assertIn("latest / active", rendered_html)
            self.assertIn("mark-generation-ready", rendered_html)
            self.assertIn("mark-generation-failed", rendered_html)
            self.assertNotIn(
                "Latest replacement failed. Harbor is still serving the older preview.",
                rendered_html,
            )

    def test_harbor_previews_render_status_page_calls_out_no_generation_yet(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                main,
                [
                    "harbor-previews",
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
                "Harbor has created this preview record, but the first generation has not been requested yet.",
                rendered_html,
            )
            self.assertIn("Preview route (not live yet)", rendered_html)
            self.assertIn("Open anchor pull request", rendered_html)
            self.assertIn("Latest generation", rendered_html)
            self.assertIn("Not created yet", rendered_html)
            self.assertNotIn("Open preview URL", rendered_html)

    def test_harbor_previews_render_status_page_calls_out_no_serving_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            output_file = Path(temporary_directory_name) / "harbor-status.html"
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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
                "Harbor has generation evidence for this preview, but nothing is serving yet.",
                rendered_html,
            )
            self.assertIn("Preview route (not serving yet)", rendered_html)
            self.assertIn("Open anchor pull request", rendered_html)
            self.assertIn("Health unavailable", rendered_html)
            self.assertIn("Latest generation", rendered_html)
            self.assertIn("hgen_01jabc_1", rendered_html)
            self.assertNotIn("Open preview URL", rendered_html)

    def test_harbor_previews_show_failed_latest_keeps_serving_generation(self) -> None:
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
                    latest_manifest_fingerprint="harbor-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="harbor-manifest-002",
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
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_show_destroyed_preview_retains_evidence(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_write_preview_creates_record_from_request(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
                + "\n",
                encoding="utf-8",
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
                    main,
                    [
                        "harbor-previews",
                        "write-preview",
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
            self.assertEqual(record.preview_label, "opw/tenant-opw/pr-123")
            self.assertEqual(
                record.canonical_url,
                "https://harbor.example/previews/opw/tenant-opw/pr-123",
            )

    def test_harbor_previews_write_preview_reuses_existing_identity_and_created_at(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
                + "\n",
                encoding="utf-8",
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
                    main,
                    [
                        "harbor-previews",
                        "write-preview",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            record = store.read_preview_record("hpr_legacy")
            self.assertEqual(record.preview_id, "hpr_legacy")
            self.assertEqual(record.created_at, "2026-04-10T10:00:00Z")
            self.assertEqual(record.updated_at, "2026-04-13T12:30:00Z")
            self.assertEqual(record.state, "paused")

    def test_harbor_previews_write_preview_fails_closed_when_base_url_missing(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
                + "\n",
                encoding="utf-8",
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
                    main,
                    [
                        "harbor-previews",
                        "write-preview",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("HARBOR_PREVIEW_BASE_URL", result.output)

    def test_harbor_previews_write_generation_assigns_next_sequence(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
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
                        "resolved_manifest_fingerprint": "harbor-manifest-002",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "write-generation",
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

    def test_harbor_previews_write_generation_fails_when_preview_missing(self) -> None:
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
                        "resolved_manifest_fingerprint": "harbor-manifest-001",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "write-generation",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No Harbor preview found", result.output)

    def test_harbor_previews_request_generation_updates_preview_and_generation_together(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
                + "\n",
                encoding="utf-8",
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
                    manifest_fingerprint="harbor-manifest-001",
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
                        "resolved_manifest_fingerprint": "harbor-manifest-002",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "request-generation",
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

    def test_harbor_previews_mark_generation_ready_cuts_over_serving_generation(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hpr_01jabc-generation-0002",
                    preview_id="hpr_01jabc",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="harbor-manifest-002",
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
                        "resolved_manifest_fingerprint": "harbor-manifest-002",
                        "artifact_id": "artifact-opw-124",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "mark-generation-ready",
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

    def test_harbor_previews_mark_generation_failed_keeps_existing_serving_generation(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hpr_01jabc-generation-0002",
                    preview_id="hpr_01jabc",
                    sequence=2,
                    state="deploying",
                    manifest_fingerprint="harbor-manifest-002",
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
                        "resolved_manifest_fingerprint": "harbor-manifest-002",
                        "artifact_id": "artifact-opw-124",
                        "failure_stage": "deploying",
                        "failure_summary": "Replacement generation failed during deploy.",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "mark-generation-failed",
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

    def test_harbor_previews_destroy_preview_clears_runtime_links(self) -> None:
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
                main,
                [
                    "harbor-previews",
                    "destroy-preview",
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

    def test_harbor_previews_ingest_pr_event_enables_preview_when_label_added(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            "schema_version = 1\n"
                            'baseline_channel = "testing"\n'
                            "```\n"
                        ),
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "enable_preview")
            self.assertEqual(payload["decision"]["resolved_context"], "opw")
            self.assertTrue(payload["decision"]["anchor_repo_eligible"])
            self.assertFalse(payload["decision"]["context_resolution_required"])
            self.assertTrue(payload["decision"]["manifest_resolved"])
            self.assertEqual(payload["request_metadata"]["status"], "valid")
            self.assertEqual(payload["request_metadata"]["metadata"]["baseline_channel"], "testing")
            self.assertEqual(payload["mutation"]["command"], "request-generation")
            self.assertFalse(payload["mutation"]["manifest_resolution_required"])
            self.assertEqual(payload["mutation"]["preview_request"]["context"], "opw")
            self.assertEqual(payload["mutation"]["preview_request"]["created_at"], "2026-04-13T12:15:00Z")
            self.assertEqual(payload["mutation"]["generation_request"]["baseline_release_tuple_id"], "opw-testing-2026-04-13")
            self.assertEqual(payload["mutation"]["generation_request"]["source_map"][0]["git_sha"], "aaaa1111")
            self.assertEqual(
                payload["mutation"]["generation_request"]["requested_reason"],
                "github_pr_event_enable_preview",
            )
            self.assertEqual(
                payload["mutation"]["generation_request"]["requested_at"],
                "2026-04-13T12:15:00Z",
            )
            self.assertEqual(payload["manifest"]["baseline_release_tuple_id"], "opw-testing-2026-04-13")
            self.assertIsNone(payload["preview"])

    def test_harbor_previews_ingest_pr_event_refreshes_existing_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record(preview_id="hpr_01jabc", state="active"))
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "synchronize",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:16:00Z",
                        "pr_body": "No Harbor metadata yet.",
                        "state": "open",
                        "head_sha": "bbbb2222",
                        "label_names": ["harbor-preview"],
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "refresh_preview")
            self.assertFalse(payload["decision"]["context_resolution_required"])
            self.assertTrue(payload["decision"]["manifest_resolved"])
            self.assertEqual(payload["request_metadata"]["status"], "missing")
            self.assertEqual(payload["mutation"]["command"], "request-generation")
            self.assertEqual(payload["mutation"]["preview_request"]["created_at"], "")
            self.assertEqual(payload["mutation"]["preview_request"]["updated_at"], "2026-04-13T12:16:00Z")
            self.assertEqual(
                payload["mutation"]["generation_request"]["requested_reason"],
                "github_pr_event_refresh_preview",
            )
            self.assertEqual(payload["preview"]["preview_id"], "hpr_01jabc")

    def test_harbor_previews_ingest_pr_event_keeps_companion_requests_unresolved(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            "schema_version = 1\n"
                            "\n"
                            "[[companions]]\n"
                            'repo = "shared-addons"\n'
                            "pr_number = 456\n"
                            "```\n"
                        ),
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertFalse(payload["decision"]["manifest_resolved"])
            self.assertIsNone(payload["manifest"])
            self.assertTrue(payload["mutation"]["manifest_resolution_required"])
            self.assertIn("generation_request_seed", payload["mutation"])

    def test_harbor_previews_ingest_pr_event_resolves_allowlisted_companion_when_lookup_succeeds(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            "schema_version = 1\n"
                            "\n"
                            "[[companions]]\n"
                            'repo = "shared-addons"\n'
                            "pr_number = 456\n"
                            "```\n"
                        ),
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch("control_plane.workflows.harbor.resolve_harbor_github_token", return_value="token"),
                patch(
                    "control_plane.workflows.harbor.fetch_github_pull_request_head",
                    return_value=(
                        "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
                        "https://github.com/every/shared-addons/pull/456",
                    ),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertTrue(payload["decision"]["manifest_resolved"])
            self.assertEqual(payload["manifest"]["source_map"][1]["selection"], "companion")
            self.assertEqual(
                payload["mutation"]["generation_request"]["companion_summaries"][0]["repo"],
                "shared-addons",
            )

    def test_harbor_previews_ingest_pr_event_apply_writes_preview_and_generation(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertTrue(payload["apply"]["applied"])
            self.assertEqual(payload["apply"]["command"], "request-generation")
            self.assertEqual(payload["feedback"]["status"], "preview_updated")
            self.assertEqual(
                payload["feedback"]["canonical_url"],
                "https://harbor.example/previews/opw/tenant-opw/pr-123",
            )
            self.assertEqual(
                payload["feedback"]["manifest_fingerprint"],
                payload["manifest"]["resolved_manifest_fingerprint"],
            )
            self.assertIn(
                "https://harbor.example/previews/opw/tenant-opw/pr-123",
                payload["feedback"]["comment_markdown"],
            )
            self.assertIn(
                payload["manifest"]["resolved_manifest_fingerprint"],
                payload["feedback"]["comment_markdown"],
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record(payload["apply"]["result"]["preview_id"])
            generation = store.read_preview_generation_record(payload["apply"]["result"]["generation_id"])
            self.assertEqual(preview.state, "pending")
            self.assertEqual(preview.active_generation_id, generation.generation_id)
            self.assertEqual(generation.resolved_manifest_fingerprint, payload["manifest"]["resolved_manifest_fingerprint"])

    def test_harbor_previews_ingest_pr_event_apply_reports_noop_when_manifest_unresolved(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            "schema_version = 1\n"
                            "\n"
                            "[[companions]]\n"
                            'repo = "shared-addons"\n'
                            "pr_number = 456\n"
                            "```\n"
                        ),
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertFalse(payload["apply"]["applied"])
            self.assertEqual(payload["apply"]["reason"], "manifest_resolution_required")
            self.assertEqual(payload["feedback"]["status"], "preview_unresolved")
            self.assertEqual(payload["feedback"]["apply_state"], "noop")
            self.assertIn("companion pull request head SHA", payload["feedback"]["detail"])
            self.assertIn("shared-addons#456", payload["feedback"]["comment_markdown"])
            store = FilesystemRecordStore(state_dir=state_dir)
            self.assertEqual(store.list_preview_records(), ())

    def test_harbor_previews_ingest_pr_event_emits_destroy_intent_for_closed_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record(preview_id="hpr_01jabc", state="active"))
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "closed",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:17:00Z",
                        "pr_body": "No Harbor metadata needed for close.",
                        "state": "closed",
                        "merged": True,
                        "head_sha": "cccc3333",
                        "label_names": ["harbor-preview"],
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "destroy_preview")
            self.assertEqual(payload["mutation"]["command"], "destroy-preview")
            self.assertEqual(payload["mutation"]["destroy_request"]["context"], "opw")
            self.assertEqual(
                payload["mutation"]["destroy_request"]["destroy_reason"],
                "pull_request_merged",
            )

    def test_harbor_previews_ingest_pr_event_apply_destroys_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record(preview_id="hpr_01jabc", state="active"))
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "closed",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:17:00Z",
                        "state": "closed",
                        "merged": False,
                        "head_sha": "cccc3333",
                        "label_names": ["harbor-preview"],
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertTrue(payload["apply"]["applied"])
            preview = store.read_preview_record(payload["apply"]["result"]["preview_id"])
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "pull_request_closed")

    def test_harbor_previews_ingest_pr_event_delivers_feedback_by_creating_comment(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch("control_plane.workflows.harbor.resolve_harbor_github_token", return_value="token"),
                patch("control_plane.workflows.harbor.find_github_issue_comment_by_marker", return_value=None),
                patch(
                    "control_plane.workflows.harbor.create_github_issue_comment",
                    return_value={
                        "id": 987,
                        "html_url": "https://github.com/every/tenant-opw/pull/123#issuecomment-987",
                    },
                ) as create_comment,
            ):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--deliver-feedback",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertTrue(payload["feedback_delivery"]["delivered"])
            self.assertEqual(payload["feedback_delivery"]["action"], "created_comment")
            create_kwargs = create_comment.call_args.kwargs
            self.assertIn("harbor-control-plane:pr-feedback", create_kwargs["body"])
            self.assertIn("Harbor resolved preview inputs", create_kwargs["body"])

    def test_harbor_previews_ingest_pr_event_delivers_feedback_by_updating_existing_comment(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch("control_plane.workflows.harbor.resolve_harbor_github_token", return_value="token"),
                patch(
                    "control_plane.workflows.harbor.find_github_issue_comment_by_marker",
                    return_value={"id": 321, "body": "<!-- harbor-control-plane:pr-feedback -->\nold"},
                ),
                patch(
                    "control_plane.workflows.harbor.update_github_issue_comment",
                    return_value={
                        "id": 321,
                        "html_url": "https://github.com/every/tenant-opw/pull/123#issuecomment-321",
                    },
                ) as update_comment,
            ):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--deliver-feedback",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertTrue(payload["feedback_delivery"]["delivered"])
            self.assertEqual(payload["feedback_delivery"]["action"], "updated_comment")
            self.assertEqual(payload["feedback_delivery"]["comment_id"], 321)
            update_kwargs = update_comment.call_args.kwargs
            self.assertIn("harbor-control-plane:pr-feedback", update_kwargs["body"])

    def test_harbor_previews_ingest_pr_event_feedback_delivery_fails_closed_without_token(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "occurred_at": "2026-04-13T12:15:00Z",
                        "state": "open",
                        "head_sha": "aaaa1111",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--deliver-feedback",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertFalse(payload["feedback_delivery"]["delivered"])
            self.assertEqual(payload["feedback_delivery"]["reason"], "github_token_missing")

    def test_harbor_previews_ingest_github_webhook_adapts_pull_request_event_and_reuses_apply_flow(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "github-webhook.json"
            webhook_payload = _github_pull_request_webhook_payload()
            input_file.write_text(json.dumps(webhook_payload), encoding="utf-8")

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--delivery-id",
                        "gh-delivery-123",
                        "--signature-256",
                        _github_webhook_signature(webhook_payload),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["webhook"]["event_name"], "pull_request")
            self.assertEqual(payload["decision"]["action"], "enable_preview")
            self.assertTrue(payload["apply"]["applied"])
            self.assertEqual(payload["feedback"]["status"], "preview_updated")
            self.assertEqual(payload["event"]["action_label"], "harbor-preview")
            self.assertEqual(payload["event"]["occurred_at"], "2026-04-13T12:15:00Z")
            self.assertEqual(payload["webhook"]["delivery"]["delivery_id"], "gh-delivery-123")
            self.assertEqual(payload["webhook"]["delivery"]["delivery_source"], "github-webhook")

    def test_harbor_previews_ingest_github_webhook_adapts_closed_pull_request_destroy_intent(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record(preview_id="hpr_01jabc", state="active"))
            input_file = control_plane_root / "github-webhook.json"
            webhook_payload = _github_pull_request_webhook_payload(
                action="closed",
                state="closed",
                merged=True,
                labels=[{"name": "harbor-preview"}],
            )
            input_file.write_text(json.dumps(webhook_payload), encoding="utf-8")

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--signature-256",
                        _github_webhook_signature(webhook_payload),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "destroy_preview")
            self.assertEqual(
                payload["mutation"]["destroy_request"]["destroyed_at"],
                "2026-04-13T12:17:00Z",
            )
            self.assertEqual(
                payload["mutation"]["destroy_request"]["destroy_reason"],
                "pull_request_merged",
            )

    def test_harbor_previews_ingest_github_webhook_fails_closed_for_unsupported_event_name(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            input_file = Path(temporary_directory_name) / "github-webhook.json"
            input_file.write_text(
                json.dumps(_github_pull_request_webhook_payload()),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "ingest-github-webhook",
                    "--input-file",
                    str(input_file),
                    "--event-name",
                    "issues",
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("event_name='pull_request'", result.output)

    def test_harbor_previews_ingest_github_webhook_fails_closed_for_malformed_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            input_file = Path(temporary_directory_name) / "github-webhook.json"
            malformed_payload = _github_pull_request_webhook_payload()
            malformed_payload["repository"] = {}
            input_file.write_text(json.dumps(malformed_payload), encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "ingest-github-webhook",
                    "--input-file",
                    str(input_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("requires string field 'name'", result.output)

    def test_harbor_previews_ingest_github_webhook_rejects_invalid_signature(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            input_file = control_plane_root / "github-webhook.json"
            webhook_payload = _github_pull_request_webhook_payload()
            input_file.write_text(json.dumps(webhook_payload), encoding="utf-8")

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-github-webhook",
                        "--input-file",
                        str(input_file),
                        "--signature-256",
                        "sha256=deadbeef",
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("signature verification failed", result.output)

    def test_harbor_previews_ingest_github_webhook_allows_explicit_unsigned_bypass(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "github-webhook.json"
            webhook_payload = _github_pull_request_webhook_payload()
            input_file.write_text(json.dumps(webhook_payload), encoding="utf-8")

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--allow-unsigned",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["webhook"]["signature_verification"]["mode"], "bypass")
            self.assertFalse(payload["webhook"]["signature_verification"]["verified"])

    def test_harbor_previews_replay_github_webhook_reuses_verified_webhook_flow(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "github-webhook-replay.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_text = json.dumps(webhook_payload)
            input_file.write_text(
                json.dumps(
                    _github_webhook_replay_envelope(
                        payload_text=payload_text,
                        signature_256=_github_webhook_signature(webhook_payload),
                        delivery_id="replay-456",
                        delivery_source="local-capture",
                    )
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "replay-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["webhook_replay"]["event_name"], "pull_request")
            self.assertEqual(payload["webhook_replay"]["delivery_id"], "replay-456")
            self.assertEqual(payload["webhook_replay"]["delivery_source"], "local-capture")
            self.assertEqual(payload["decision"]["action"], "enable_preview")
            self.assertTrue(payload["apply"]["applied"])
            self.assertTrue(payload["webhook"]["signature_verification"]["verified"])
            self.assertEqual(payload["webhook"]["delivery"]["delivery_id"], "replay-456")
            self.assertEqual(payload["webhook"]["delivery"]["delivery_source"], "local-capture")

    def test_harbor_previews_build_github_webhook_replay_envelope_emits_minimal_envelope(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope = json.loads(result.output)
            self.assertEqual(envelope["event_name"], "pull_request")
            self.assertEqual(envelope["adapter"], "github_webhook")
            self.assertTrue(envelope["allow_unsigned"])
            self.assertEqual(envelope["payload_text"], json.dumps(webhook_payload))
            self.assertNotIn("capture", envelope)

    def test_harbor_previews_build_github_webhook_replay_envelope_round_trips_into_replay(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            payload_file = control_plane_root / "github-webhook.json"
            headers_file = control_plane_root / "github-webhook-headers.json"
            evidence_file = control_plane_root / "github-webhook-evidence.json"
            envelope_file = control_plane_root / "github-webhook-replay.json"
            webhook_payload = _github_pull_request_webhook_payload()
            signature_256 = _github_webhook_signature(webhook_payload)
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")
            headers_file.write_text(
                json.dumps(
                    {
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "replay-builder-123",
                        "X-Hub-Signature-256": signature_256,
                        "X-GitHub-Hook-ID": "hook-42",
                    }
                ),
                encoding="utf-8",
            )
            evidence_file.write_text(
                json.dumps(
                    {
                        "capture_file": "fixtures/github/replay-builder-123.http",
                        "operator_note": "captured during local webhook debugging",
                    }
                ),
                encoding="utf-8",
            )

            build_result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--recorded-at",
                    "2026-04-13T12:30:00Z",
                    "--capture-source",
                    "local-http-capture",
                    "--evidence-file",
                    str(evidence_file),
                    "--output-file",
                    str(envelope_file),
                ],
            )

            self.assertEqual(build_result.exit_code, 0, msg=build_result.output)
            built_envelope = json.loads(envelope_file.read_text(encoding="utf-8"))
            self.assertEqual(built_envelope["payload_text"], json.dumps(webhook_payload))
            self.assertEqual(built_envelope["capture"]["source"], "local-http-capture")
            self.assertEqual(
                built_envelope["capture"]["headers"]["X-GitHub-Delivery"],
                "replay-builder-123",
            )
            self.assertEqual(
                built_envelope["capture"]["evidence"]["capture_file"],
                "fixtures/github/replay-builder-123.http",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                replay_result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "replay-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(envelope_file),
                        "--apply",
                    ],
                )

            self.assertEqual(replay_result.exit_code, 0, msg=replay_result.output)
            replay_payload = json.loads(replay_result.output)
            self.assertEqual(replay_payload["decision"]["action"], "enable_preview")
            self.assertTrue(replay_payload["apply"]["applied"])
            self.assertTrue(replay_payload["webhook"]["signature_verification"]["verified"])
            self.assertEqual(replay_payload["webhook"]["delivery"]["delivery_id"], "replay-builder-123")
            self.assertEqual(
                replay_payload["webhook_replay"]["capture"]["source"],
                "local-http-capture",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_non_string_headers(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            headers_file = Path(temporary_directory_name) / "github-webhook-headers.json"
            payload_file.write_text(
                json.dumps(_github_pull_request_webhook_payload()),
                encoding="utf-8",
            )
            headers_file.write_text(
                json.dumps({"X-GitHub-Event": ["pull_request"]}),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("map header names to string values", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_redacts_sensitive_headers_file_values(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            headers_file = Path(temporary_directory_name) / "github-webhook-headers.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")
            headers_file.write_text(
                json.dumps(
                    {
                        "Authorization": "Bearer super-secret-token",
                        "baggage": "userId=123,tenant=cm,debug=true",
                        "Cookie": "session=secret-cookie",
                        "CF-Connecting-IP": "203.0.113.43",
                        "Proxy-Authorization": "Basic c2VjcmV0",
                        "True-Client-IP": "203.0.113.44",
                        "Forwarded": "for=203.0.113.43;proto=https;host=harbor.example",
                        "X-Forwarded-For": "203.0.113.43",
                        "X-Forwarded-Host": "harbor.example",
                        "X-Real-IP": "203.0.113.45",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "redacted-headers-123",
                        "Via": "1.1 proxy.example",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Authorization"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["baggage"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Cookie"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["CF-Connecting-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Proxy-Authorization"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["True-Client-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Forwarded"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Forwarded-For"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Forwarded-Host"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Real-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-GitHub-Delivery"],
                "redacted-headers-123",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Via"],
                "1.1 proxy.example",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_accepts_http_capture_file(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            http_capture_file = control_plane_root / "github-webhook.http"
            envelope_file = control_plane_root / "github-webhook-replay.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_text = json.dumps(webhook_payload)
            signature_256 = _github_webhook_signature(webhook_payload)
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        f"X-Hub-Signature-256: {signature_256}",
                        "X-HTTP-Method-Override: POST",
                        "Transfer-Encoding: identity",
                        "Content-Encoding: identity",
                        f"Content-Length: {len(payload_text.encode('utf-8'))}",
                        "Content-Type: application/json; charset=utf-8",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            build_result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--recorded-at",
                    "2026-04-13T12:40:00Z",
                    "--capture-source",
                    "saved-http-capture",
                    "--output-file",
                    str(envelope_file),
                ],
            )

            self.assertEqual(build_result.exit_code, 0, msg=build_result.output)
            built_envelope = json.loads(envelope_file.read_text(encoding="utf-8"))
            self.assertEqual(built_envelope["payload_text"], payload_text)
            self.assertEqual(
                built_envelope["capture"]["headers"]["X-GitHub-Delivery"],
                "http-capture-123",
            )
            self.assertEqual(built_envelope["capture"]["source"], "saved-http-capture")
            self.assertEqual(
                built_envelope["capture"]["evidence"]["http_request"]["request_line"],
                "POST /github/webhook HTTP/1.1",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                replay_result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "replay-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(envelope_file),
                        "--apply",
                    ],
                )

            self.assertEqual(replay_result.exit_code, 0, msg=replay_result.output)
            replay_payload = json.loads(replay_result.output)
            self.assertEqual(replay_payload["decision"]["action"], "enable_preview")
            self.assertTrue(replay_payload["webhook"]["signature_verification"]["verified"])
            self.assertEqual(replay_payload["webhook"]["delivery"]["delivery_id"], "http-capture-123")
            self.assertEqual(replay_payload["webhook_replay"]["capture"]["source"], "saved-http-capture")
            self.assertEqual(
                replay_payload["webhook_replay"]["capture"]["evidence"]["http_request"]["request_line"],
                "POST /github/webhook HTTP/1.1",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_correlation_headers_file_values(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            headers_file = Path(temporary_directory_name) / "github-webhook-headers.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")
            headers_file.write_text(
                json.dumps(
                    {
                        "CF-Ray": "8d2f4bb7c9d7abcd-SJC",
                        "X-Amzn-Trace-Id": "Root=1-abcdef01-23456789abcdef0123456789",
                        "X-Request-Id": "trace-123",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "correlation-headers-123",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["CF-Ray"],
                "8d2f4bb7c9d7abcd-SJC",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Amzn-Trace-Id"],
                "Root=1-abcdef01-23456789abcdef0123456789",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Request-Id"],
                "trace-123",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_distributed_tracing_headers_file_values(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            headers_file = Path(temporary_directory_name) / "github-webhook-headers.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")
            headers_file.write_text(
                json.dumps(
                    {
                        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
                        "tracestate": "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE",
                        "b3": "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "trace-context-123",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["traceparent"],
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["tracestate"],
                "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["b3"],
                "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_vendor_tracing_headers_file_values(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            payload_file = Path(temporary_directory_name) / "github-webhook.json"
            headers_file = Path(temporary_directory_name) / "github-webhook-headers.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_file.write_text(json.dumps(webhook_payload), encoding="utf-8")
            headers_file.write_text(
                json.dumps(
                    {
                        "sentry-trace": "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
                        "x-cloud-trace-context": "105445aa7843bc8bf206b12000100000/1;o=1",
                        "x-datadog-trace-id": "1234567890123456789",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "vendor-tracing-123",
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--payload-file",
                    str(payload_file),
                    "--headers-file",
                    str(headers_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["sentry-trace"],
                "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["x-cloud-trace-context"],
                "105445aa7843bc8bf206b12000100000/1;o=1",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["x-datadog-trace-id"],
                "1234567890123456789",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_observational_http_capture_headers(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            http_capture_file = control_plane_root / "github-webhook.http"
            envelope_file = control_plane_root / "github-webhook-replay.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_text = json.dumps(webhook_payload)
            signature_256 = _github_webhook_signature(webhook_payload)
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "Via: 1.1 proxy.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-via-123",
                        f"X-Hub-Signature-256: {signature_256}",
                        f"Content-Length: {len(payload_text.encode('utf-8'))}",
                        "Content-Type: application/json; charset=utf-8",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            build_result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--capture-source",
                    "saved-http-capture",
                    "--output-file",
                    str(envelope_file),
                ],
            )

            self.assertEqual(build_result.exit_code, 0, msg=build_result.output)
            built_envelope = json.loads(envelope_file.read_text(encoding="utf-8"))
            self.assertEqual(
                built_envelope["capture"]["headers"]["Via"],
                "1.1 proxy.example",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                replay_result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "replay-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(envelope_file),
                    ],
                )

            self.assertEqual(replay_result.exit_code, 0, msg=replay_result.output)
            replay_payload = json.loads(replay_result.output)
            self.assertEqual(replay_payload["decision"]["action"], "enable_preview")
            self.assertEqual(
                replay_payload["webhook_replay"]["capture"]["headers"]["Via"],
                "1.1 proxy.example",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_http_capture_correlation_headers(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "CF-Ray: 8d2f4bb7c9d7abcd-SJC",
                        "X-Amzn-Trace-Id: Root=1-abcdef01-23456789abcdef0123456789",
                        "X-Request-Id: trace-123",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: correlation-http-123",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["CF-Ray"],
                "8d2f4bb7c9d7abcd-SJC",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Amzn-Trace-Id"],
                "Root=1-abcdef01-23456789abcdef0123456789",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Request-Id"],
                "trace-123",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_http_capture_distributed_tracing_headers(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
                        "tracestate: rojo=00f067aa0ba902b7,congo=t61rcWkgMzE",
                        "b3: 4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: trace-context-http-123",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["traceparent"],
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["tracestate"],
                "rojo=00f067aa0ba902b7,congo=t61rcWkgMzE",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["b3"],
                "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_preserves_http_capture_vendor_tracing_headers(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "sentry-trace: 4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
                        "x-cloud-trace-context: 105445aa7843bc8bf206b12000100000/1;o=1",
                        "x-datadog-trace-id: 1234567890123456789",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: vendor-tracing-http-123",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["sentry-trace"],
                "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["x-cloud-trace-context"],
                "105445aa7843bc8bf206b12000100000/1;o=1",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["x-datadog-trace-id"],
                "1234567890123456789",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_http_capture_cache_control(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        "Cache-Control: no-cache",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "Cache-Control declarations are unsupported",
                result.output,
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_redacts_sensitive_http_capture_headers(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "Authorization: Bearer super-secret-token",
                        "baggage: userId=123,tenant=cm,debug=true",
                        "Cookie: session=secret-cookie",
                        "CF-Connecting-IP: 203.0.113.43",
                        "Proxy-Authorization: Basic c2VjcmV0",
                        "True-Client-IP: 203.0.113.44",
                        "Forwarded: for=203.0.113.43;proto=https;host=harbor.example",
                        "X-Forwarded-For: 203.0.113.43",
                        "X-Forwarded-Proto: https",
                        "X-Real-IP: 203.0.113.45",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: redacted-http-123",
                        "Via: 1.1 proxy.example",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            envelope_payload = json.loads(result.output)
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Authorization"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["baggage"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Cookie"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["CF-Connecting-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Proxy-Authorization"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["True-Client-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Forwarded"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Forwarded-For"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Forwarded-Proto"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-Real-IP"],
                "[redacted]",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["X-GitHub-Delivery"],
                "redacted-http-123",
            )
            self.assertEqual(
                envelope_payload["capture"]["headers"]["Via"],
                "1.1 proxy.example",
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_http_capture_upgrade(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        "Upgrade: websocket",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "Upgrade declarations are unsupported",
                result.output,
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_http_capture_te(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        "TE: trailers",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "TE declarations are unsupported",
                result.output,
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_http_capture_keep_alive(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        "Keep-Alive: timeout=5, max=1000",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "Keep-Alive declarations are unsupported",
                result.output,
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_http_capture_proxy_connection(
        self,
    ) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "Host: harbor.example",
                        "X-GitHub-Event: pull_request",
                        "X-GitHub-Delivery: http-capture-123",
                        "Proxy-Connection: keep-alive",
                        "",
                        json.dumps(_github_pull_request_webhook_payload()),
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn(
                "Proxy-Connection declarations are unsupported",
                result.output,
            )

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_conflicting_http_request_evidence(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            evidence_file = Path(temporary_directory_name) / "github-webhook-evidence.json"
            webhook_payload = _github_pull_request_webhook_payload()
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "",
                        json.dumps(webhook_payload),
                    ]
                ),
                encoding="utf-8",
            )
            evidence_file.write_text(
                json.dumps(
                    {
                        "http_request": {
                            "request_line": "POST /other HTTP/1.1",
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                    "--evidence-file",
                    str(evidence_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("request_line conflicts", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_mismatched_http_capture_content_length(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Content-Length: 1",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Content-Length does not match", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_non_integer_http_capture_content_length(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Content-Length: abc",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Content-Length header must be an integer", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_non_json_http_capture_content_type(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Content-Type: text/plain",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Content-Type must be JSON", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_conflicting_http_method_override(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "X-HTTP-Method-Override: PATCH",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("X-HTTP-Method-Override must not conflict", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_unsupported_transfer_encoding(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Transfer-Encoding: chunked",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Transfer-Encoding is unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_unsupported_content_encoding(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Content-Encoding: gzip",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Content-Encoding is unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_trailer_declarations(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Trailer: X-GitHub-Delivery",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Trailer declarations are unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_expect_declarations(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Expect: 100-continue",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Expect declarations are unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_connection_declarations(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Connection: keep-alive",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Connection declarations are unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_pragma_declarations(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            payload_text = json.dumps(_github_pull_request_webhook_payload())
            http_capture_file.write_text(
                "\n".join(
                    [
                        "POST /github/webhook HTTP/1.1",
                        "X-GitHub-Event: pull_request",
                        "Pragma: no-cache",
                        "",
                        payload_text,
                    ]
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Pragma declarations are unsupported", result.output)

    def test_harbor_previews_build_github_webhook_replay_envelope_rejects_malformed_http_capture(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            http_capture_file = Path(temporary_directory_name) / "github-webhook.http"
            http_capture_file.write_text(
                "GET /github/webhook HTTP/1.1\nX-GitHub-Event pull_request\n\n{}",
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "build-github-webhook-replay-envelope",
                    "--http-capture-file",
                    str(http_capture_file),
                    "--allow-unsigned",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("must start with a POST request line", result.output)

    def test_harbor_previews_replay_github_webhook_accepts_richer_capture_shape(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            _write_runtime_environments_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "github-webhook-replay.json"
            webhook_payload = _github_pull_request_webhook_payload()
            payload_text = json.dumps(webhook_payload)
            signature_256 = _github_webhook_signature(webhook_payload)
            input_file.write_text(
                json.dumps(
                    _github_webhook_replay_envelope(
                        event_name="",
                        delivery_id="",
                        delivery_source="",
                        payload_text=payload_text,
                        signature_256="",
                        capture={
                            "recorded_at": "2026-04-13T12:20:00Z",
                            "source": "captured-http-request",
                            "headers": {
                                "X-GitHub-Event": "pull_request",
                                "X-GitHub-Delivery": "replay-789",
                                "X-Hub-Signature-256": signature_256,
                                "X-GitHub-Hook-ID": "hook-42",
                            },
                            "evidence": {
                                "capture_file": "fixtures/github/replay-789.json",
                                "operator_note": "captured from staging tunnel",
                            },
                        },
                    )
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "replay-github-webhook",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "enable_preview")
            self.assertTrue(payload["apply"]["applied"])
            self.assertTrue(payload["webhook"]["signature_verification"]["verified"])
            self.assertEqual(payload["webhook"]["delivery"]["delivery_id"], "replay-789")
            self.assertEqual(payload["webhook"]["delivery"]["delivery_source"], "captured-http-request")
            self.assertEqual(payload["webhook_replay"]["event_name"], "pull_request")
            self.assertEqual(payload["webhook_replay"]["capture"]["recorded_at"], "2026-04-13T12:20:00Z")
            self.assertEqual(
                payload["webhook_replay"]["capture"]["headers"]["X-GitHub-Hook-ID"],
                "hook-42",
            )
            self.assertEqual(
                payload["webhook_replay"]["capture"]["evidence"]["capture_file"],
                "fixtures/github/replay-789.json",
            )

    def test_harbor_previews_replay_github_webhook_fails_closed_for_conflicting_capture_headers(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            input_file = Path(temporary_directory_name) / "github-webhook-replay.json"
            input_file.write_text(
                json.dumps(
                    _github_webhook_replay_envelope(
                        payload_text=json.dumps(_github_pull_request_webhook_payload()),
                        allow_unsigned=True,
                        capture={
                            "headers": {
                                "X-GitHub-Event": "issues",
                            }
                        },
                    )
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "replay-github-webhook",
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("conflicts with capture header X-GitHub-Event", result.output)

    def test_harbor_previews_replay_github_webhook_fails_closed_for_signed_envelope_without_payload_text(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            input_file = Path(temporary_directory_name) / "github-webhook-replay.json"
            input_file.write_text(
                json.dumps(
                    _github_webhook_replay_envelope(
                        payload=_github_pull_request_webhook_payload(),
                        signature_256="sha256=deadbeef",
                    )
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
                    "replay-github-webhook",
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("requires payload_text", result.output)

    def test_harbor_previews_ingest_pr_event_ignores_infra_or_companion_repos(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            _write_release_tuples_file(control_plane_root)
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "shared-addons",
                        "pr_number": 456,
                        "pr_url": "https://github.com/every/shared-addons/pull/456",
                        "occurred_at": "2026-04-13T12:18:00Z",
                        "pr_body": (
                            "```harbor-preview\n"
                            "schema_version = 1\n"
                            "\n"
                            "[[companions]]\n"
                            'repo = "tenant-cm"\n'
                            "pr_number = 10\n"
                            "```\n"
                        ),
                        "state": "open",
                        "head_sha": "bbbb2222",
                        "label_names": ["harbor-preview"],
                        "action_label": "harbor-preview",
                    }
                ),
                encoding="utf-8",
            )

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "harbor-previews",
                        "ingest-pr-event",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "ignore")
            self.assertFalse(payload["decision"]["anchor_repo_eligible"])
            self.assertEqual(payload["decision"]["resolved_context"], "")
            self.assertEqual(payload["request_metadata"]["status"], "invalid")
            self.assertIsNone(payload["mutation"])

    def test_harbor_previews_list_keeps_destroyed_previews_visible_and_filters_by_context(self) -> None:
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
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="hpr_01jxyz",
                    anchor_pr_number=124,
                    anchor_pr_url="https://github.com/every/tenant-opw/pull/124",
                    preview_label="opw/tenant-opw/pr-124",
                    canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-124",
                    state="destroyed",
                    active_generation_id="",
                    serving_generation_id="",
                    latest_generation_id="hgen_01jxyz_1",
                    latest_manifest_fingerprint="harbor-manifest-099",
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
                    manifest_fingerprint="harbor-manifest-099",
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
                    canonical_url="https://harbor.example/previews/cm/tenant-cm/pr-10",
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
                    manifest_fingerprint="harbor-manifest-cm-001",
                    artifact_id="artifact-cm-010",
                )
            )

            result = runner.invoke(
                main,
                [
                    "harbor-previews",
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

    def test_harbor_previews_history_marks_latest_and_serving_generations(self) -> None:
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
                    latest_manifest_fingerprint="harbor-manifest-002",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_1",
                    sequence=1,
                    state="ready",
                    manifest_fingerprint="harbor-manifest-001",
                    artifact_id="artifact-opw-123",
                )
            )
            store.write_preview_generation_record(
                _generation_record(
                    "hgen_01jabc_2",
                    sequence=2,
                    state="failed",
                    manifest_fingerprint="harbor-manifest-002",
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
                main,
                [
                    "harbor-previews",
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
