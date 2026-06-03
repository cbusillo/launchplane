import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
)
from control_plane.contracts.protected_artifacts import build_protected_artifact_set
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.storage.filesystem import FilesystemRecordStore


def _deploy(target_name: str) -> DeploymentEvidence:
    return DeploymentEvidence(
        target_name=target_name,
        target_type="application",
        deploy_mode="dokploy",
        status="pass",
        started_at="2026-06-03T20:00:00Z",
        finished_at="2026-06-03T20:01:00Z",
    )


def _runtime_identity(
    *,
    product: str,
    context: str,
    instance: str,
    artifact_id: str,
    source_git_ref: str,
    image_reference: str,
    environment_kind: str = "stable",
    preview_id: str = "",
    preview_generation_id: str = "",
) -> RuntimeIdentity:
    return RuntimeIdentity(
        product=product,
        context=context,
        instance=instance,
        environment_kind=environment_kind,
        deployment_record_id=f"deployment-{context}-{instance}",
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        image_reference=image_reference,
        preview_id=preview_id,
        preview_generation_id=preview_generation_id,
        deployed_at="2026-06-03T20:01:00Z",
    )


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="verireel",
        display_name="VeriReel",
        repository="cbusillo/verireel",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/verireel-app"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(instance="testing", context="verireel"),
            ProductLaneProfile(instance="prod", context="verireel"),
        ),
        preview=ProductPreviewProfile(enabled=True, context="verireel-testing"),
        updated_at="2026-06-03T20:00:00Z",
        source="test",
    )


def _manifest(artifact_id: str, source_git_ref: str, tag: str) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id=artifact_id,
        source_commit=source_git_ref,
        enterprise_base_digest="sha256:base123",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/verireel-app",
            digest=f"sha256:{artifact_id.replace('-', '')[:16]}",
            tags=(tag,),
        ),
    )


def _inventory(*, instance: str, artifact_id: str, source_git_ref: str) -> EnvironmentInventory:
    image_reference = f"ghcr.io/cbusillo/verireel-app:sha-{source_git_ref}"
    return EnvironmentInventory(
        context="verireel",
        instance=instance,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        source_git_ref=source_git_ref,
        deploy=_deploy(f"verireel-{instance}"),
        runtime_identity=_runtime_identity(
            product="verireel",
            context="verireel",
            instance=instance,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            image_reference=image_reference,
        ),
        updated_at="2026-06-03T20:02:00Z",
        deployment_record_id=f"deployment-verireel-{instance}",
    )


def _active_preview() -> PreviewRecord:
    return PreviewRecord(
        preview_id="preview-verireel-pr-196",
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=196,
        anchor_pr_url="https://github.com/cbusillo/verireel/pull/196",
        preview_label="pr-196",
        canonical_url="https://pr-196.preview.example",
        state="active",
        created_at="2026-06-03T19:00:00Z",
        updated_at="2026-06-03T20:00:00Z",
        eligible_at="2026-06-03T19:00:00Z",
        active_generation_id="preview-verireel-pr-196-generation-0001",
        serving_generation_id="preview-verireel-pr-196-generation-0001",
        latest_generation_id="preview-verireel-pr-196-generation-0001",
    )


def _destroyed_preview() -> PreviewRecord:
    return _active_preview().model_copy(
        update={
            "preview_id": "preview-verireel-pr-195",
            "anchor_pr_number": 195,
            "state": "destroyed",
            "destroyed_at": "2026-06-03T20:00:00Z",
            "active_generation_id": "preview-verireel-pr-195-generation-0001",
            "serving_generation_id": "preview-verireel-pr-195-generation-0001",
            "latest_generation_id": "preview-verireel-pr-195-generation-0001",
        }
    )


def _preview_generation(
    preview: PreviewRecord,
    *,
    generation_id: str = "",
    artifact_id: str = "",
    state: PreviewGenerationState = "ready",
) -> PreviewGenerationRecord:
    resolved_generation_id = generation_id or preview.serving_generation_id
    resolved_artifact_id = artifact_id or f"artifact-{preview.preview_id}"
    source_git_ref = "abcdef1234567890abcdef1234567890abcdef12"
    return PreviewGenerationRecord(
        generation_id=resolved_generation_id,
        preview_id=preview.preview_id,
        sequence=1,
        state=state,
        requested_reason="pull_request",
        requested_at="2026-06-03T19:00:00Z",
        ready_at="2026-06-03T19:15:00Z",
        finished_at="2026-06-03T19:15:00Z",
        resolved_manifest_fingerprint="fingerprint-1",
        artifact_id=resolved_artifact_id,
        anchor_summary=PreviewPullRequestSummary(
            repo="cbusillo/verireel",
            pr_number=preview.anchor_pr_number,
            head_sha=source_git_ref,
            pr_url=preview.anchor_pr_url,
        ),
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
        runtime_identity=_runtime_identity(
            product="verireel",
            context=preview.context,
            instance=preview.preview_label,
            artifact_id=resolved_artifact_id,
            source_git_ref=source_git_ref,
            image_reference=(
                f"ghcr.io/cbusillo/verireel-app:{preview.preview_label}-sha-{source_git_ref[:8]}"
            ),
            environment_kind="preview",
            preview_id=preview.preview_id,
            preview_generation_id=resolved_generation_id,
        ),
    )


def _preview_feedback(preview: PreviewRecord) -> PreviewPrFeedbackRecord:
    return PreviewPrFeedbackRecord(
        feedback_id="preview-feedback-verireel-pr-196",
        product="verireel",
        context=preview.context,
        source="workflow",
        requested_at="2026-06-03T19:20:00Z",
        repository="cbusillo/verireel",
        anchor_repo=preview.anchor_repo,
        anchor_pr_number=preview.anchor_pr_number,
        anchor_pr_url=preview.anchor_pr_url,
        status="ready",
        marker="<!-- verireel-preview-control -->",
        comment_markdown="<!-- verireel-preview-control -->\nReady.",
        preview_url=preview.canonical_url,
        immutable_image_reference="ghcr.io/cbusillo/verireel-app:pr-196-sha-abcdef12",
        refresh_image_reference="ghcr.io/cbusillo/verireel-app:pr-196",
        revision="abcdef12",
        run_url="https://github.com/cbusillo/verireel/actions/runs/1",
        delivery_status="delivered",
    )


def _seed_store(store: FilesystemRecordStore) -> None:
    store.write_product_profile_record(_profile())
    store.write_artifact_manifest(
        _manifest(
            "artifact-verireel-testing",
            "87d6d02e1c8679992667d17f2558f7be5c537821",
            "sha-87d6d02e1c8679992667d17f2558f7be5c537821",
        )
    )
    store.write_artifact_manifest(
        _manifest(
            "artifact-verireel-prod",
            "b6c883432baeaa4bf3dfe7c1c833265868da9346",
            "sha-b6c883432baeaa4bf3dfe7c1c833265868da9346",
        )
    )
    store.write_environment_inventory(
        _inventory(
            instance="testing",
            artifact_id="artifact-verireel-testing",
            source_git_ref="87d6d02e1c8679992667d17f2558f7be5c537821",
        )
    )
    store.write_environment_inventory(
        _inventory(
            instance="prod",
            artifact_id="artifact-verireel-prod",
            source_git_ref="b6c883432baeaa4bf3dfe7c1c833265868da9346",
        )
    )
    active_preview = _active_preview()
    destroyed_preview = _destroyed_preview()
    store.write_preview_record(active_preview)
    store.write_preview_record(destroyed_preview)
    store.write_preview_generation_record(_preview_generation(active_preview))
    store.write_preview_generation_record(_preview_generation(destroyed_preview))
    store.write_preview_pr_feedback_record(_preview_feedback(active_preview))
    store.write_release_tuple_record(
        ReleaseTupleRecord(
            tuple_id="verireel-prod-artifact-verireel-prod",
            context="verireel",
            channel="prod",
            artifact_id="artifact-verireel-prod",
            repo_shas={"verireel": "b6c883432baeaa4bf3dfe7c1c833265868da9346"},
            image_repository="ghcr.io/cbusillo/verireel-app",
            image_digest="sha256:artifactverireel",
            deployment_record_id="deployment-verireel-prod",
            provenance="promotion",
            promoted_from_channel="testing",
            minted_at="2026-06-03T20:02:00Z",
        )
    )


class ProtectedArtifactTests(TestCase):
    def test_build_protected_artifact_set_includes_stable_and_active_preview_refs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            _seed_store(store)

            protected = build_protected_artifact_set(store, product="verireel")

        self.assertEqual(
            protected.artifact_ids,
            (
                "artifact-preview-verireel-pr-196",
                "artifact-verireel-prod",
                "artifact-verireel-testing",
            ),
        )
        self.assertIn(
            "ghcr.io/cbusillo/verireel-app:sha-b6c883432baeaa4bf3dfe7c1c833265868da9346",
            protected.image_references,
        )
        self.assertIn("sha256:artifactverireel", protected.image_digests)
        self.assertIn(
            "ghcr.io/cbusillo/verireel-app:pr-196",
            protected.image_references,
        )
        self.assertNotIn("artifact-preview-verireel-pr-195", protected.artifact_ids)
        self.assertEqual(
            [entry.reason for entry in protected.entries],
            [
                "stable-inventory",
                "stable-inventory",
                "release-tuple",
                "active-preview-generation",
                "active-preview-feedback",
            ],
        )
        self.assertTrue(
            any("artifact-preview-verireel-pr-196" in warning for warning in protected.warnings)
        )

    def test_build_protected_artifact_set_keeps_linked_preview_rollout_images(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            _seed_store(store)
            preview = _active_preview().model_copy(
                update={
                    "active_generation_id": "preview-verireel-pr-196-generation-0002",
                    "latest_generation_id": "preview-verireel-pr-196-generation-0002",
                }
            )
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation(
                    preview,
                    generation_id="preview-verireel-pr-196-generation-0002",
                    artifact_id="artifact-preview-verireel-pr-196-generation-0002",
                    state="deploying",
                )
            )

            protected = build_protected_artifact_set(store, product="verireel")

        self.assertIn("artifact-preview-verireel-pr-196", protected.artifact_ids)
        self.assertIn("artifact-preview-verireel-pr-196-generation-0002", protected.artifact_ids)
        preview_generation_entries = [
            entry for entry in protected.entries if entry.reason == "active-preview-generation"
        ]
        self.assertEqual(len(preview_generation_entries), 2)

    def test_build_protected_artifact_set_warns_for_unresolved_live_inventory(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            store.write_product_profile_record(_profile())
            store.write_environment_inventory(
                EnvironmentInventory(
                    context="verireel",
                    instance="prod",
                    source_git_ref="b6c883432baeaa4bf3dfe7c1c833265868da9346",
                    deploy=_deploy("verireel-prod"),
                    updated_at="2026-06-03T20:02:00Z",
                    deployment_record_id="deployment-verireel-prod",
                )
            )

            protected = build_protected_artifact_set(store, product="verireel")

        self.assertEqual(protected.artifact_ids, ())
        self.assertEqual(protected.image_references, ())
        self.assertTrue(any("environment_inventory" in warning for warning in protected.warnings))

    def test_protected_artifacts_cli_outputs_json_for_cleanup_consumers(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            _seed_store(FilesystemRecordStore(state_dir=state_dir))

            result = runner.invoke(
                main,
                [
                    "artifacts",
                    "protected",
                    "--state-dir",
                    str(state_dir),
                    "--local-rehearsal",
                    "--product",
                    "verireel",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["product"], "verireel")
        self.assertIn("artifact-verireel-prod", payload["artifact_ids"])
        self.assertIn(
            "ghcr.io/cbusillo/verireel-app:pr-196-sha-abcdef12",
            payload["image_references"],
        )

    def test_protected_artifacts_cli_requires_authoritative_state_by_default(
        self,
    ) -> None:
        result = CliRunner().invoke(main, ["artifacts", "protected", "--product", "verireel"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("requires --database-url or LAUNCHPLANE_DATABASE_URL", result.output)
