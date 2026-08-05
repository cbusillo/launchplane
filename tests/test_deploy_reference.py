import unittest

from pydantic import ValidationError

from control_plane.contracts.deploy_reference import (
    docker_image_digest,
    docker_image_repository,
    docker_image_tag,
    validate_provider_deploy_reference,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.contracts.promotion_record import HealthcheckEvidence


class DeployReferenceContractTests(unittest.TestCase):
    def test_validates_digest_identity_and_non_floating_provider_tag(self) -> None:
        deploy_reference = validate_provider_deploy_reference(
            artifact_id="ghcr.io/every/verireel-app@sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            deploy_reference="ghcr.io/every/verireel-app:sha-abcdef1234567890abcdef1234567890abcdef12",
            label="test deploy",
        )

        self.assertEqual(
            deploy_reference,
            "ghcr.io/every/verireel-app:sha-abcdef1234567890abcdef1234567890abcdef12",
        )
        self.assertEqual(docker_image_repository(deploy_reference), "ghcr.io/every/verireel-app")
        self.assertEqual(
            docker_image_tag(deploy_reference),
            "sha-abcdef1234567890abcdef1234567890abcdef12",
        )
        self.assertEqual(
            docker_image_digest(
                "ghcr.io/every/verireel-app@sha256:"
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )

    def test_rejects_deploy_reference_without_digest_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "artifact_id to be a digest-pinned"):
            validate_provider_deploy_reference(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef",
                deploy_reference="ghcr.io/every/verireel-app:sha-abcdef",
                label="test deploy",
            )

    def test_rejects_digest_and_floating_deploy_references(self) -> None:
        artifact_id = (
            "ghcr.io/every/verireel-app@sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        for deploy_reference in (
            artifact_id,
            "ghcr.io/every/verireel-app:latest",
            "ghcr.io/every/verireel-app:prod",
        ):
            with self.subTest(deploy_reference=deploy_reference):
                with self.assertRaisesRegex(ValueError, "non-floating image tag"):
                    validate_provider_deploy_reference(
                        artifact_id=artifact_id,
                        deploy_reference=deploy_reference,
                        label="test deploy",
                    )

    def test_rejects_repository_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "same image repository"):
            validate_provider_deploy_reference(
                artifact_id="ghcr.io/every/verireel-app@sha256:"
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                deploy_reference="ghcr.io/every/other-app:sha-abcdef1234567890",
                label="test deploy",
            )

    def test_ship_request_preserves_backward_compatible_tag_only_artifact(self) -> None:
        request = ShipRequest(
            artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
            context="verireel",
            instance="testing",
            source_git_ref="abcdef1234567890",
            target_name="ver-testing-app",
            target_type="application",
            provider_id="dokploy",
            target_category="application",
            provider_target_type="application",
            deploy_mode="dokploy-application-api",
            verify_health=False,
            destination_health=HealthcheckEvidence(status="skipped"),
        )

        self.assertEqual(request.artifact_id, "ghcr.io/every/verireel-app:sha-abcdef1234567890")
        self.assertEqual(request.deploy_reference, "")

    def test_ship_request_validates_optional_deploy_reference(self) -> None:
        with self.assertRaisesRegex(ValidationError, "same image repository"):
            ShipRequest(
                artifact_id="ghcr.io/every/verireel-app@sha256:"
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                deploy_reference="ghcr.io/every/other-app:sha-abcdef1234567890",
                context="verireel",
                instance="testing",
                source_git_ref="abcdef1234567890",
                target_name="ver-testing-app",
                target_type="application",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                deploy_mode="dokploy-application-api",
                verify_health=False,
                destination_health=HealthcheckEvidence(status="skipped"),
            )


if __name__ == "__main__":
    unittest.main()
