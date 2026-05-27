import unittest

from pydantic import ValidationError

from control_plane.contracts.artifact_publish_inputs import (
    GenericArtifactPublishInputsRequest,
    GenericArtifactPublishInputsResult,
)


class GenericArtifactPublishInputsContractTests(unittest.TestCase):
    def test_request_normalizes_shared_publish_scope_fields(self) -> None:
        request = GenericArtifactPublishInputsRequest(
            context=" CM ",
            instance=" TESTING ",
            preview_slug=" pr-28 ",
            source_git_ref=" abcdef1234567890 ",
        )

        self.assertEqual(request.context, "cm")
        self.assertEqual(request.instance, "testing")
        self.assertEqual(request.preview_slug, "pr-28")
        self.assertEqual(request.source_git_ref, "abcdef1234567890")

    def test_result_filters_empty_environment_values(self) -> None:
        result = GenericArtifactPublishInputsResult(
            context="cm",
            instance="testing",
            environment={
                " ODOO_BASE_RUNTIME_IMAGE ": " ghcr.io/cbusillo/runtime:19 ",
                "EMPTY_VALUE": " ",
                " ": "ignored",
            },
        )

        self.assertEqual(
            result.environment,
            {"ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19"},
        )

    def test_result_requires_image_repository_for_image_tag(self) -> None:
        with self.assertRaises(ValidationError):
            GenericArtifactPublishInputsResult(
                context="cm",
                instance="testing",
                image_tag="cm-pr-28-abcdef12-amd64",
            )


if __name__ == "__main__":
    unittest.main()
