from __future__ import annotations

from pathlib import Path
import unittest

from tests.support.workflows import load_workflow


_PUBLISH_WORKER = (
    "cbusillo/launchplane/.github/workflows/reusable-odoo-artifact-publish.yml@"
    "e605d8ab9ec26950247233c4237d65ea8b44a6d6"
)


class OdooArtifactPublishWorkflowTests(unittest.TestCase):
    def test_dispatch_wrapper_pins_landed_authoritative_worker(self) -> None:
        workflow = load_workflow(".github/workflows/odoo-artifact-publish.yml")

        trigger = workflow.data["on"]
        self.assertIsInstance(trigger, dict)
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        dispatch = trigger["workflow_dispatch"]
        self.assertIsInstance(dispatch, dict)
        assert isinstance(dispatch, dict)
        inputs = dispatch["inputs"]
        self.assertIsInstance(inputs, dict)
        assert isinstance(inputs, dict)
        self.assertEqual(
            set(inputs),
            {"product", "context", "instance", "source_git_ref", "confirmation"},
        )
        self.assertNotIn("product_repository", inputs)
        self.assertEqual(set(workflow.jobs), {"publish"})
        self.assertEqual(workflow.job_uses("publish"), _PUBLISH_WORKER)
        self.assertEqual(
            workflow.job_permissions("publish"),
            {"contents": "read", "id-token": "write", "packages": "write"},
        )
        self.assertEqual(workflow.steps("publish"), ())
        self.assertEqual(
            workflow.job("publish")["with"],
            {
                "product": "${{ inputs.product }}",
                "context": "${{ inputs.context }}",
                "instance": "${{ inputs.instance }}",
                "source_git_ref": "${{ inputs.source_git_ref }}",
                "confirmation": "${{ inputs.confirmation }}",
                "launchplane_url": "${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
                "launchplane_audience": "${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",
            },
        )
        self.assertEqual(
            workflow.job("publish")["secrets"],
            {
                "ODOO_GHCR_PUBLISH_TOKEN": "${{ secrets.ODOO_GHCR_PUBLISH_TOKEN }}",
                "ODOO_SOURCE_GITHUB_TOKEN": "${{ secrets.ODOO_SOURCE_GITHUB_TOKEN }}",
            },
        )
        text = Path(workflow.path).read_text(encoding="utf-8")
        self.assertNotIn("launchplane-request@", text)
        self.assertNotIn("runs-on:", text)


if __name__ == "__main__":
    unittest.main()
