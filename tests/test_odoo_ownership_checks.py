import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

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
    uses: cbusillo/launchplane/.github/workflows/reusable-product-driver-prod-promotion.yml@main
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

    def test_rejects_repo_local_launchplane_http_client(self) -> None:
        clients = (
            "const response = await fetch(launchplaneUrl);\n",
            "const response = await axios(launchplaneUrl, {method: 'GET'});\n",
            "const response = await axios.request({url: launchplaneUrl});\n",
            "response = requests.get(launchplane_url)\n",
            "response = requests.request('GET', launchplane_url)\n",
            "response = urlopen(launchplane_url)\n",
            'subprocess.run(["curl", launchplane_url])\n',
            "curl https://launchplane.example/v1/service/runtime\n",
        )
        for client in clients:
            with self.subTest(client=client), TemporaryDirectory() as workspace:
                workspace_root = Path(workspace)
                script = workspace_root / "odoo-tenant-cm" / "scripts" / "launchplane-client.mjs"
                script.parent.mkdir(parents=True)
                script.write_text(client, encoding="utf-8")

                result = scan_odoo_ownership_boundaries(
                    workspace_root=workspace_root,
                    repo_policies=(OdooOwnershipRepoPolicy("odoo-tenant-cm", "tenant"),),
                )

            self.assertEqual(result.status, "fail")
            self.assertEqual(result.findings[0].rule_id, "repo-local-launchplane-http-client")

    def test_allows_shared_request_action_with_descriptive_fetch_step_name(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            workflow = (
                workspace_root / "odoo-tenant-cm" / ".github" / "workflows" / "cleanup-ghcr.yml"
            )
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                """
jobs:
  cleanup:
    steps:
      - name: Fetch Launchplane protected artifact inventory
        uses: cbusillo/launchplane/.github/actions/launchplane-request@main
      - name: Curl Launchplane protected artifact inventory
        uses: cbusillo/launchplane/.github/actions/launchplane-request@main
""".lstrip(),
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("odoo-tenant-cm", "tenant"),),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.findings, ())

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

    def test_allows_descriptive_shared_prod_mutation_comments(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
            script = workspace_root / "devkit" / "scripts" / "restore.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "# Integrations whose restored settings a non-production restore keeps, comma-separated\n"
                "    # Shared production restore settings are documented here.\n",
                encoding="utf-8",
            )

            result = scan_odoo_ownership_boundaries(
                workspace_root=workspace_root,
                repo_policies=(OdooOwnershipRepoPolicy("devkit", "devkit"),),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.findings, ())

    def test_rejects_executable_shared_prod_mutations_beside_comments(self) -> None:
        commands = (
            "production restore",
            "prod deploy",
            "shared rollback",
            "production promote",
            'subprocess.run(["production", "restore"])',
            'subprocess.run(["shared", "deploy", "#label"])',
            '#restore = () => execa("odoo", ["production", "restore"])',
            "#!/usr/bin/env -S devkit production deploy",
        )
        for policy in (
            OdooOwnershipRepoPolicy("repo", "devkit"),
            OdooOwnershipRepoPolicy("repo", "retired"),
        ):
            for command in commands:
                with (
                    self.subTest(family=policy.family, command=command),
                    TemporaryDirectory() as workspace,
                ):
                    workspace_root = Path(workspace)
                    script = workspace_root / "repo" / "scripts" / "mutate.mjs"
                    script.parent.mkdir(parents=True)
                    script.write_text(
                        f"# non-production restore description\n{command} # explanatory comment\n",
                        encoding="utf-8",
                    )

                    result = scan_odoo_ownership_boundaries(
                        workspace_root=workspace_root,
                        repo_policies=(policy,),
                    )

                    self.assertEqual(result.status, "fail")
                    self.assertEqual(
                        [(finding.rule_id, finding.line) for finding in result.findings],
                        [("non-launchplane-shared-prod-mutation", 2)],
                    )

    def test_cli_returns_nonzero_for_findings(self) -> None:
        with TemporaryDirectory() as workspace:
            workspace_root = Path(workspace)
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
                    "--repository",
                    "odoo-tenant-cm",
                    "tenant",
                    "--format",
                    "json",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('"status": "fail"', result.output)
        self.assertIn("repo-local-oidc-client", result.output)

    def test_root_and_nested_source_files_are_checked(self) -> None:
        for relative in ("ops.py", "scripts/ops.py"):
            with self.subTest(path=relative), TemporaryDirectory() as workspace:
                root = Path(workspace)
                script = root / "new-tenant" / relative
                script.parent.mkdir(parents=True)
                script.write_text("ACTIONS_ID_TOKEN_REQUEST_TOKEN = token\n", encoding="utf-8")
                result = scan_odoo_ownership_boundaries(
                    workspace_root=root,
                    repo_policies=(OdooOwnershipRepoPolicy("new-tenant", "tenant"),),
                )
            self.assertEqual(result.status, "fail")
            self.assertEqual(
                [(finding.path, finding.rule_id) for finding in result.findings],
                [(relative, "repo-local-oidc-client")],
            )

    def test_requires_nonempty_unambiguous_coverage(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            for policies in (
                (),
                (
                    OdooOwnershipRepoPolicy("repo", "tenant"),
                    OdooOwnershipRepoPolicy(str(root / "repo"), "image"),
                ),
            ):
                with self.subTest(policies=policies), self.assertRaises(ValueError):
                    scan_odoo_ownership_boundaries(workspace_root=root, repo_policies=policies)

    def test_incomplete_requested_coverage_fails_with_available_root_reported(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "available").mkdir()
            (root / "available" / "build.py").write_text("print('build')\n", encoding="utf-8")
            (root / "not-directory").write_text("content", encoding="utf-8")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root,
                repo_policies=tuple(
                    OdooOwnershipRepoPolicy(name, "tenant")
                    for name in ("available", "missing", "not-directory")
                ),
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(
            [(entry.repository, entry.status) for entry in result.coverage],
            [
                ("available", "complete"),
                ("missing", "unavailable"),
                ("not-directory", "unavailable"),
            ],
        )
        self.assertEqual(result.coverage[0].scanned_file_count, 1)
        self.assertEqual(
            {finding.repository for finding in result.findings}, {"missing", "not-directory"}
        )

    def test_unreadable_source_cannot_pass(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "repo").mkdir()
            (root / "repo" / "ops.py").write_bytes(b"\xff")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "unavailable")
        self.assertEqual(result.scanned_file_count, 0)
        self.assertEqual(result.findings[0].rule_id, "file-unavailable")

    def test_failed_traversal_cannot_pass(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "repo").mkdir()
            with patch("control_plane.odoo_ownership_checks.os.walk") as walk:

                def denied_walk(*args: object, **kwargs: object) -> None:
                    onerror = kwargs["onerror"]
                    assert callable(onerror)
                    onerror(PermissionError("source directory is unreadable"))

                walk.side_effect = denied_walk
                result = scan_odoo_ownership_boundaries(
                    workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
                )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "unavailable")
        self.assertEqual(result.findings[0].rule_id, "repository-unavailable")

    def test_explicit_exceptions_report_omitted_files(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            repo = root / "tool"
            repo.mkdir()
            for relative in ("local.py", "build.py", "tests/fixture.py", "nested/local.py"):
                source = repo / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("print('local')\n", encoding="utf-8")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root,
                repo_policies=(
                    OdooOwnershipRepoPolicy("tool", "devkit", allowed_path_globs=("**/local.py",)),
                ),
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.coverage[0].scanned_file_count, 1)
        self.assertEqual(result.coverage[0].omitted_file_count, 3)

    def test_cli_checks_arbitrary_relative_and_absolute_roots_with_family_intent(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            tenant = root / "brand-new-tenant"
            image = root / "build-image"
            tenant.mkdir()
            image.mkdir()
            (tenant / "ops.py").write_text("dokploy compose apply\n", encoding="utf-8")
            (image / "build.sh").write_text("docker compose build\n", encoding="utf-8")
            result = CliRunner().invoke(
                main,
                [
                    "odoo-ownership",
                    "check",
                    "--workspace-root",
                    str(root),
                    "--repository",
                    tenant.name,
                    "tenant",
                    "--repository",
                    str(image),
                    "image",
                    "--format",
                    "json",
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        payload = json.JSONDecoder().raw_decode(result.output)[0]
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(
            [
                (entry["repository"], entry["family"], entry["status"])
                for entry in payload["coverage"]
            ],
            [(tenant.name, "tenant", "complete"), (str(image), "image", "complete")],
        )
        self.assertEqual(
            [(finding["repository"], finding["rule_id"]) for finding in payload["findings"]],
            [(tenant.name, "tenant-provider-mutation")],
        )

    def test_cli_rejects_missing_or_invalid_family_coverage(self) -> None:
        for selection in ([], ["--repository", "repo", "unknown"]):
            with self.subTest(selection=selection):
                result = CliRunner().invoke(main, ["odoo-ownership", "check", *selection])
            self.assertNotEqual(result.exit_code, 0)

    def test_cli_pass_names_only_requested_coverage(self) -> None:
        with TemporaryDirectory() as workspace:
            repo = Path(workspace) / "source"
            repo.mkdir()
            (repo / "build.py").write_text("print('build')\n", encoding="utf-8")
            result = CliRunner().invoke(
                main,
                [
                    "odoo-ownership",
                    "check",
                    "--repository",
                    str(repo),
                    "shared_addons",
                    "--format",
                    "json",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual([entry["root"] for entry in payload["coverage"]], [str(repo.resolve())])
