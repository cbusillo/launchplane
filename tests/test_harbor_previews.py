import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
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
    generate_preview_generation_id,
    generate_preview_id,
    harbor_preview_label_enabled,
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
            state_dir = control_plane_root / "state"
            input_file = control_plane_root / "pr-event.json"
            input_file.write_text(
                json.dumps(
                    {
                        "action": "labeled",
                        "repo": "tenant-opw",
                        "pr_number": 123,
                        "pr_url": "https://github.com/every/tenant-opw/pull/123",
                        "state": "open",
                        "head_sha": "aaaa1111",
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
            payload = json.loads(result.output)
            self.assertEqual(payload["decision"]["action"], "enable_preview")
            self.assertTrue(payload["decision"]["context_resolution_required"])
            self.assertIsNone(payload["preview"])

    def test_harbor_previews_ingest_pr_event_refreshes_existing_preview(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
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
                        "state": "open",
                        "head_sha": "bbbb2222",
                        "label_names": ["harbor-preview"],
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
            self.assertEqual(payload["decision"]["action"], "refresh_preview")
            self.assertFalse(payload["decision"]["context_resolution_required"])
            self.assertEqual(payload["preview"]["preview_id"], "hpr_01jabc")

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
