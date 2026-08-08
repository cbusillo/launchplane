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
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview,
    apply_launchplane_destroy_preview_if_present,
    apply_launchplane_generation_evidence,
    resolve_next_launchplane_preview_generation_identity,
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


class _BundledPreviewMutationStore(_FakePreviewMutationStore):
    def __init__(self, *, fail_evidence_write: bool = False) -> None:
        super().__init__()
        self.fail_evidence_write = fail_evidence_write
        self.preview_write_count = 0
        self.generation_write_count = 0
        self.evidence_write_count = 0

    def write_preview_record(self, record: PreviewRecord) -> str:
        self.preview_write_count += 1
        return super().write_preview_record(record)

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> str:
        self.generation_write_count += 1
        return super().write_preview_generation_record(record)

    def write_preview_generation_evidence_records(
        self,
        *,
        preview_record: PreviewRecord,
        generation_record: PreviewGenerationRecord,
    ) -> tuple[str, str]:
        self.evidence_write_count += 1
        if self.fail_evidence_write:
            raise RuntimeError("bundled write failed")
        self.generations[generation_record.generation_id] = generation_record
        self.previews[preview_record.preview_id] = preview_record
        return (
            f"generation-evidence://{generation_record.generation_id}",
            f"preview-evidence://{preview_record.preview_id}",
        )


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
    def test_next_preview_generation_identity_uses_canonical_record_ids(self) -> None:
        store = _FakePreviewMutationStore()

        first = resolve_next_launchplane_preview_generation_identity(
            record_store=store,
            context="site-testing",
            anchor_repo="cbusillo/site",
            anchor_pr_number=42,
        )
        apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(
                sequence=first.sequence,
                generation_id=first.generation_id,
            ),
        )
        second = resolve_next_launchplane_preview_generation_identity(
            record_store=store,
            context="site-testing",
            anchor_repo="cbusillo/site",
            anchor_pr_number=42,
        )

        self.assertEqual(first.preview_id, "preview-site-testing-cbusillo-site-pr-42")
        self.assertEqual(
            first.generation_id,
            "preview-site-testing-cbusillo-site-pr-42-generation-0001",
        )
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.preview_id, first.preview_id)
        self.assertEqual(
            second.generation_id,
            "preview-site-testing-cbusillo-site-pr-42-generation-0002",
        )
        self.assertEqual(second.sequence, 2)

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

    def test_generation_evidence_persists_checked_runtime_identity(self) -> None:
        store = _BundledPreviewMutationStore()
        runtime_identity = RuntimeIdentity(
            product="site",
            context="site-testing",
            instance="pr-42",
            environment_kind="preview",
            deployment_record_id="deployment-pr-42",
            artifact_id="artifact-preview-42",
            source_git_ref="abc123",
            image_reference=f"ghcr.io/cbusillo/site@sha256:{'a' * 64}",
            preview_id="pr-42",
        )

        result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(runtime_identity=runtime_identity),
        )

        generation = store.generations[str(result["generation_id"])]
        self.assertEqual(generation.runtime_identity, runtime_identity)
        self.assertEqual(store.evidence_write_count, 1)

    def test_generation_evidence_replay_preserves_checked_runtime_identity(self) -> None:
        store = _BundledPreviewMutationStore()
        runtime_identity = RuntimeIdentity(
            product="site",
            context="site-testing",
            instance="pr-42",
            environment_kind="preview",
            deployment_record_id="deployment-pr-42",
            artifact_id="artifact-preview-42",
            source_git_ref="abc123",
            image_reference=f"ghcr.io/cbusillo/site@sha256:{'a' * 64}",
            preview_id="pr-42",
        )
        first = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(runtime_identity=runtime_identity),
        )

        apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(),
        )

        generation = store.generations[str(first["generation_id"])]
        self.assertEqual(generation.runtime_identity, runtime_identity)

    def test_generation_evidence_uses_bundled_store_write_when_available(self) -> None:
        store = _BundledPreviewMutationStore()

        result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(),
        )

        self.assertEqual(store.preview_write_count, 0)
        self.assertEqual(store.generation_write_count, 0)
        self.assertEqual(store.evidence_write_count, 1)
        self.assertEqual(
            result["generation_path"],
            "generation-evidence://preview-site-testing-cbusillo-site-pr-42-generation-0001",
        )
        self.assertEqual(
            result["preview_path"],
            "preview-evidence://preview-site-testing-cbusillo-site-pr-42",
        )
        preview = store.previews["preview-site-testing-cbusillo-site-pr-42"]
        self.assertEqual(preview.state, "active")
        self.assertEqual(preview.serving_generation_id, result["generation_id"])

    def test_generation_evidence_bundled_write_failure_leaves_no_partial_preview(
        self,
    ) -> None:
        store = _BundledPreviewMutationStore(fail_evidence_write=True)

        with self.assertRaises(RuntimeError):
            apply_launchplane_generation_evidence(
                control_plane_root_path=Path("/launchplane"),
                record_store=store,
                preview_request=_preview_request(),
                generation_request=_generation_request(),
            )

        self.assertEqual(store.preview_write_count, 0)
        self.assertEqual(store.generation_write_count, 0)
        self.assertEqual(store.evidence_write_count, 1)
        self.assertEqual(store.previews, {})
        self.assertEqual(store.generations, {})

    def test_generation_evidence_retry_with_different_explicit_sequence_creates_new_generation(
        self,
    ) -> None:
        store = _BundledPreviewMutationStore()
        apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(),
        )

        result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(sequence=2),
        )

        self.assertEqual(
            result["generation_id"],
            "preview-site-testing-cbusillo-site-pr-42-generation-0002",
        )
        self.assertEqual(
            store.generations["preview-site-testing-cbusillo-site-pr-42-generation-0001"].sequence,
            1,
        )
        self.assertEqual(
            store.generations["preview-site-testing-cbusillo-site-pr-42-generation-0002"].sequence,
            2,
        )

    def test_generation_evidence_preserves_existing_sequence_for_explicit_generation_id(
        self,
    ) -> None:
        store = _BundledPreviewMutationStore()
        first_result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(),
        )

        result = apply_launchplane_generation_evidence(
            control_plane_root_path=Path("/launchplane"),
            record_store=store,
            preview_request=_preview_request(),
            generation_request=_generation_request(
                generation_id=str(first_result["generation_id"]),
                sequence=2,
            ),
        )

        self.assertEqual(result["generation_id"], first_result["generation_id"])
        self.assertEqual(
            store.generations[str(first_result["generation_id"])].sequence,
            1,
        )

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

    def test_destroy_preview_if_present_reports_missing_preview(self) -> None:
        result = apply_launchplane_destroy_preview_if_present(
            record_store=_FakePreviewMutationStore(),
            request=PreviewDestroyMutationRequest(
                context="site-testing",
                anchor_repo="cbusillo/site",
                anchor_pr_number=42,
                destroyed_at="2026-05-03T00:05:00Z",
                destroy_reason="closed",
            ),
        )

        self.assertEqual(result, {"transition": "destroyed_missing_preview"})

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
