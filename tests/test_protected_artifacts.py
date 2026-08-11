import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.protected_artifacts import build_protected_artifact_set
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.protected_artifacts import (
    _active_preview,
    _deploy,
    _preview_generation,
    _profile,
    seed_protected_artifact_store,
)


class ProtectedArtifactTests(TestCase):
    def test_build_protected_artifact_set_includes_stable_and_active_preview_refs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            seed_protected_artifact_store(store)

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
            seed_protected_artifact_store(store)
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

    def test_build_protected_artifact_set_keeps_retiring_profile_artifacts_protected(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            seed_protected_artifact_store(store)
            active_profile = store.read_product_profile_record("verireel")
            retiring_profile = active_profile.model_copy(
                update={
                    "lifecycle_state": "retiring",
                    "preview": active_profile.preview.model_copy(update={"enabled": False}),
                }
            )
            store.compare_and_write_product_profile_record(
                expected_record=active_profile,
                replacement_record=retiring_profile,
            )

            protected = build_protected_artifact_set(store, product="verireel")

        self.assertIn("artifact-verireel-prod", protected.artifact_ids)
        self.assertIn("artifact-verireel-testing", protected.artifact_ids)

    def test_protected_artifacts_cli_outputs_json_for_cleanup_consumers(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            seed_protected_artifact_store(FilesystemRecordStore(state_dir=state_dir))

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
