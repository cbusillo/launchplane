from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.generic_promotion_inputs import (
    build_generic_promotion_backup_record_id,
    GenericPromotionInputsRequest,
    resolve_generic_promotion_inputs,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


class _Store:
    def __init__(
        self,
        *,
        release_tuple: ReleaseTupleRecord | None = None,
        manifest: ArtifactIdentityManifest | None = None,
    ) -> None:
        self.release_tuple = release_tuple
        self.manifest = manifest

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord:
        del context_name, channel_name
        if self.release_tuple is None:
            raise FileNotFoundError("missing release tuple")
        return self.release_tuple

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        del artifact_id
        if self.manifest is None:
            raise FileNotFoundError("missing artifact manifest")
        return self.manifest


class GenericPromotionInputsTests(unittest.TestCase):
    def test_resolves_current_source_tuple_into_promotion_inputs(self) -> None:
        result = resolve_generic_promotion_inputs(
            record_store=_Store(
                release_tuple=_release_tuple(),
                manifest=_artifact_manifest(),
            ),
            request=GenericPromotionInputsRequest(
                context="CM",
                from_instance="testing",
                to_instance="prod",
                request_id="run-123-attempt-1",
            ),
        )

        self.assertEqual(result.input_status, "ready")
        self.assertEqual(result.context, "cm")
        self.assertEqual(result.artifact_id, "artifact-cm-new")
        self.assertEqual(result.source_git_ref, "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        self.assertEqual(result.image_repository, "ghcr.io/cbusillo/example")
        self.assertEqual(result.image_digest, "sha256:tuple")

    def test_blocks_when_source_tuple_is_missing(self) -> None:
        result = resolve_generic_promotion_inputs(
            record_store=_Store(manifest=_artifact_manifest()),
            request=GenericPromotionInputsRequest(context="cm", request_id="run-123"),
        )

        self.assertEqual(result.input_status, "blocked")
        self.assertIn("current testing release tuple", result.error_message)

    def test_blocks_when_artifact_manifest_is_missing(self) -> None:
        result = resolve_generic_promotion_inputs(
            record_store=_Store(release_tuple=_release_tuple()),
            request=GenericPromotionInputsRequest(context="cm", request_id="run-123"),
        )

        self.assertEqual(result.input_status, "blocked")
        self.assertEqual(result.artifact_id, "artifact-cm-new")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        self.assertIn("artifact manifest", result.error_message)

    def test_requires_different_source_and_destination_instances(self) -> None:
        with self.assertRaises(ValidationError):
            GenericPromotionInputsRequest(
                context="cm",
                from_instance="testing",
                to_instance="testing",
            )

    def test_backup_record_id_slugifies_operator_request_id(self) -> None:
        self.assertEqual(
            build_generic_promotion_backup_record_id(
                context="CM",
                instance="Prod",
                request_id="Run 123 / Attempt 1",
            ),
            "backup-gate-cm-prod-run-123-attempt-1",
        )


def _release_tuple() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="cm-testing-artifact-cm-new",
        context="cm",
        channel="testing",
        artifact_id="artifact-cm-new",
        repo_shas={"example": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb"},
        image_repository="ghcr.io/cbusillo/example",
        image_digest="sha256:tuple",
        deployment_record_id="deployment-cm-testing",
        provenance="ship",
        minted_at="2026-05-27T18:00:00Z",
    )


def _artifact_manifest(
    source_commit: str = "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest.model_validate(
        {
            "artifact_id": "artifact-cm-new",
            "source_commit": source_commit,
            "enterprise_base_digest": "sha256:enterprise",
            "image": {
                "repository": "ghcr.io/cbusillo/example",
                "digest": "sha256:manifest",
            },
        }
    )


if __name__ == "__main__":
    unittest.main()
