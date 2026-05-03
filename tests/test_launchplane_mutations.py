import unittest
from pathlib import Path

import click

from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
    upsert_launchplane_preview_from_request,
)


class _FakePreviewMutationStore:
    def __init__(self) -> None:
        self.previews: dict[str, PreviewRecord] = {}
        self.generations: dict[str, PreviewGenerationRecord] = {}

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
            for record in self.previews.values()
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        records.sort(key=lambda record: (record.updated_at, record.preview_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_record(self, record: PreviewRecord) -> str:
        self.previews[record.preview_id] = record
        return f"preview://{record.preview_id}"

    def list_preview_generation_records(
        self, *, preview_id: str = "", limit: int | None = None
    ) -> tuple[PreviewGenerationRecord, ...]:
        records = [
            record
            for record in self.generations.values()
            if not preview_id or record.preview_id == preview_id
        ]
        records.sort(key=lambda record: (record.sequence, record.generation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> str:
        self.generations[record.generation_id] = record
        return f"generation://{record.generation_id}"


def _preview_request(**updates: object) -> PreviewMutationRequest:
    payload: dict[str, object] = {
        "context": "site-testing",
        "anchor_repo": "cbusillo/site",
        "anchor_pr_number": 42,
        "anchor_pr_url": "https://github.com/cbusillo/site/pull/42",
        "canonical_url": "https://preview.example/previews/site-testing/cbusillo/site/pr-42",
        "state": "pending",
        "created_at": "2026-05-03T00:00:00Z",
        "updated_at": "2026-05-03T00:00:00Z",
        "eligible_at": "2026-05-03T00:00:00Z",
    }
    payload.update(updates)
    return PreviewMutationRequest.model_validate(payload)


def _generation_request(**updates: object) -> PreviewGenerationMutationRequest:
    payload: dict[str, object] = {
        "context": "site-testing",
        "anchor_repo": "cbusillo/site",
        "anchor_pr_number": 42,
        "anchor_pr_url": "https://github.com/cbusillo/site/pull/42",
        "anchor_head_sha": "abc123",
        "state": "ready",
        "requested_reason": "pr-updated",
        "requested_at": "2026-05-03T00:01:00Z",
        "ready_at": "2026-05-03T00:03:00Z",
        "resolved_manifest_fingerprint": "sha256:manifest",
        "artifact_id": "artifact-preview-42",
        "deploy_status": "pass",
        "verify_status": "pass",
        "overall_health_status": "pass",
    }
    payload.update(updates)
    return PreviewGenerationMutationRequest.model_validate(payload)


class LaunchplaneMutationTests(unittest.TestCase):
    def test_upsert_preview_uses_structural_store_boundary(self) -> None:
        store = _FakePreviewMutationStore()

        preview = upsert_launchplane_preview_from_request(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            request=_preview_request(),
        )

        self.assertEqual(preview.preview_id, "preview-site-testing-cbusillo-site-pr-42")
        self.assertEqual(store.previews[preview.preview_id], preview)

    def test_generation_evidence_transitions_preview_ready(self) -> None:
        store = _FakePreviewMutationStore()

        result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(),
        )

        self.assertEqual(result["transition"], "ready")
        self.assertEqual(
            result["generation_id"], "preview-site-testing-cbusillo-site-pr-42-generation-0001"
        )
        preview = store.previews["preview-site-testing-cbusillo-site-pr-42"]
        self.assertEqual(preview.state, "active")
        self.assertEqual(preview.serving_generation_id, result["generation_id"])

    def test_destroy_preview_requires_existing_preview(self) -> None:
        store = _FakePreviewMutationStore()

        with self.assertRaises(click.ClickException):
            apply_launchplane_destroy_preview(
                record_store=store,
                request=PreviewDestroyMutationRequest(
                    context="site-testing",
                    anchor_repo="cbusillo/site",
                    anchor_pr_number=42,
                    destroyed_at="2026-05-03T00:05:00Z",
                    destroy_reason="closed",
                ),
            )

    def test_destroy_preview_records_destroyed_state(self) -> None:
        store = _FakePreviewMutationStore()
        preview = upsert_launchplane_preview_from_request(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            request=_preview_request(),
        )

        result = apply_launchplane_destroy_preview(
            record_store=store,
            request=PreviewDestroyMutationRequest(
                context="site-testing",
                anchor_repo="cbusillo/site",
                anchor_pr_number=42,
                destroyed_at="2026-05-03T00:05:00Z",
                destroy_reason="closed",
            ),
        )

        self.assertEqual(result["transition"], "destroyed")
        destroyed_preview = store.previews[preview.preview_id]
        self.assertEqual(destroyed_preview.state, "destroyed")
        self.assertEqual(destroyed_preview.destroy_reason, "closed")


if __name__ == "__main__":
    unittest.main()
