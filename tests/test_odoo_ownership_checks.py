from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.odoo_ownership_checks import (
    OdooOwnershipRepoPolicy,
    scan_odoo_ownership_boundaries,
)


class OdooOwnershipChecksTests(TestCase):
    def test_allows_shared_launchplane_connectors(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            workflow = workspace_root / "odoo-tenant-cm" / ".github" / "workflows" / "odoo.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
jobs:
  preview:
    permissions:
      id-token: write
    steps:
      - uses: cbusillo/launchplane/.github/actions/launchplane-request@main
        with:
          launchplane-url: https://launchplane.shinycomputers.com
          audience: launchplane.shinycomputers.com
  stable:
    permissions:
      id-token: write
    uses: cbusillo/launchplane/.github/workflows/reusable-odoo-prod-promotion.yml@main
""".lstrip(),
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("odoo-tenant-cm", "tenant"),),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.findings, ())

    def test_rejects_repo_local_oidc_client(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            script = workspace_root / "odoo-tenant-cm" / "scripts" / "launchplane-client.mjs"
            script.parent.mkdir(parents=True)
            script.write_text(
                "const token = await core.getIDToken('launchplane.shinycomputers.com');\n",
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("odoo-tenant-cm", "tenant"),),
            )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.findings[0].rule_id, "repo-local-oidc-client")

    def test_rejects_tenant_provider_mutation(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            workflow = workspace_root / "odoo-tenant-opw" / ".github" / "workflows" / "deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
jobs:
  deploy:
    steps:
      - run: dokploy compose apply --app opw-prod
""".lstrip(),
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("odoo-tenant-opw", "tenant"),),
            )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.findings[0].rule_id, "tenant-provider-mutation")

    def test_rejects_devkit_shared_prod_mutation_flow(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            command = workspace_root / "odoo-devkit" / "odoo_devkit" / "remote_runtime.py"
            command.parent.mkdir(parents=True)
            command.write_text(
                "def deploy_prod_override() -> None:\n    print('prod override apply')\n",
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("odoo-devkit", "devkit"),),
            )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.findings[0].rule_id, "non-launchplane-shared-prod-mutation")

    def test_cli_returns_nonzero_for_findings(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            for repository in (
                "odoo-tenant-opw",
                "odoo-docker",
                "odoo-enterprise-docker",
                "odoo-shared-addons",
                "odoo-devkit",
            ):
                (workspace_root / repository).mkdir()
            script = workspace_root / "odoo-tenant-cm" / "scripts" / "client.js"
            script.parent.mkdir(parents=True)
            script.write_text("await getIDToken();\n", encoding="utf-8")

            result = CliRunner().invoke(
                main,
                [
                    "odoo-ownership",
                    "check",
                    "--workspace-root",
                    str(workspace_root),
                    "--format",
                    "json",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('"status": "fail"', result.output)
        self.assertIn("repo-local-oidc-client", result.output)


class OdooOwnershipDocsTests(TestCase):
    def test_product_repo_contract_documents_regression_check(self) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")

        self.assertIn("uv run launchplane odoo-ownership check", product_repo_contract)
        self.assertIn(
            "cbusillo/launchplane/.github/actions/launchplane-request@main", product_repo_contract
        )
        self.assertIn("reusable-odoo-*.yml@main", product_repo_contract)
