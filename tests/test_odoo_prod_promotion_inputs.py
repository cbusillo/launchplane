from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.workflows.odoo_prod_promotion_inputs import (
    OdooProdPromotionInputsRequest,
    build_odoo_prod_backup_record_id,
    resolve_odoo_prod_promotion_inputs,
)


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


class OdooProdPromotionInputsTest(unittest.TestCase):
    def test_resolves_current_testing_tuple_into_promotion_inputs(self) -> None:
        result = resolve_odoo_prod_promotion_inputs(
            record_store=_Store(
                release_tuple=_release_tuple(),
                manifest=_artifact_manifest(),
            ),
            request=OdooProdPromotionInputsRequest(
                context="CM",
                request_id="run-123-attempt-1",
            ),
        )

        self.assertEqual(result.input_status, "ready")
        self.assertEqual(result.context, "cm")
        self.assertEqual(result.artifact_id, "artifact-cm-new")
        self.assertEqual(result.source_git_ref, "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb")
        self.assertEqual(result.backup_record_id, "backup-gate-cm-prod-run-123-attempt-1")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        self.assertEqual(result.image_repository, "ghcr.io/cbusillo/odoo-tenant-cm")
        self.assertEqual(result.image_digest, "sha256:tuple")

    def test_blocks_when_testing_tuple_is_missing(self) -> None:
        result = resolve_odoo_prod_promotion_inputs(
            record_store=_Store(manifest=_artifact_manifest()),
            request=OdooProdPromotionInputsRequest(context="cm", request_id="run-123"),
        )

        self.assertEqual(result.input_status, "blocked")
        self.assertEqual(result.artifact_id, "")
        self.assertIn("current testing release tuple", result.error_message)

    def test_blocks_when_artifact_manifest_is_missing(self) -> None:
        result = resolve_odoo_prod_promotion_inputs(
            record_store=_Store(release_tuple=_release_tuple()),
            request=OdooProdPromotionInputsRequest(context="cm", request_id="run-123"),
        )

        self.assertEqual(result.input_status, "blocked")
        self.assertEqual(result.artifact_id, "artifact-cm-new")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        self.assertIn("artifact manifest", result.error_message)

    def test_requires_testing_to_prod(self) -> None:
        with self.assertRaises(ValidationError):
            OdooProdPromotionInputsRequest(
                context="cm",
                from_instance="prod",
                to_instance="testing",
                request_id="run-123",
            )

    def test_backup_record_id_slugifies_operator_request_id(self) -> None:
        self.assertEqual(
            build_odoo_prod_backup_record_id(
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
        repo_shas={"tenant-cm": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb"},
        image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
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
                "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                "digest": "sha256:manifest",
            },
        }
    )


if __name__ == "__main__":
    unittest.main()
