import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.storage.filesystem import FilesystemRecordStore


def _preview_record(
    *,
    state: str = "active",
    active_generation_id: str = "hgen_01jabc_1",
    serving_generation_id: str = "hgen_01jabc_1",
    latest_generation_id: str = "hgen_01jabc_1",
    latest_manifest_fingerprint: str = "harbor-manifest-001",
    destroy_after: str = "2026-04-20T12:14:00Z",
    destroyed_at: str = "",
    destroy_reason: str = "",
) -> PreviewRecord:
    return PreviewRecord(
        preview_id="hpr_01jabc",
        context="opw",
        anchor_repo="tenant-opw",
        anchor_pr_number=123,
        anchor_pr_url="https://github.com/every/tenant-opw/pull/123",
        preview_label="opw/tenant-opw/pr-123",
        canonical_url="https://harbor.example/previews/opw/tenant-opw/pr-123",
        state=state,
        created_at="2026-04-13T12:00:00Z",
        updated_at="2026-04-13T12:14:00Z",
        eligible_at="2026-04-13T12:00:00Z",
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
        preview_id="hpr_01jabc",
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
            PreviewSourceRecord(repo="tenant-opw", git_sha="aaaa1111", selection="anchor"),
            PreviewSourceRecord(repo="shared-addons", git_sha="bbbb2222", selection="companion"),
        ),
        anchor_summary=PreviewPullRequestSummary(
            repo="tenant-opw",
            pr_number=123,
            head_sha="aaaa1111",
            pr_url="https://github.com/every/tenant-opw/pull/123",
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


if __name__ == "__main__":
    unittest.main()
