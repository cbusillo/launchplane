import json
import os
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
        self.assertEqual(json.JSONDecoder().raw_decode(result.output)[0]["status"], "fail")
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
            self.assertIn(
                (relative, "repo-local-oidc-client"),
                [(finding.path, finding.rule_id) for finding in result.findings],
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
            source = root / "repo" / "source"
            source.mkdir(parents=True)
            source.chmod(0)
            try:
                if os.access(source, os.R_OK):
                    self.skipTest("This process can read a directory without permission bits.")
                result = scan_odoo_ownership_boundaries(
                    workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
                )
            finally:
                source.chmod(0o700)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "unavailable")
        self.assertEqual(result.findings[0].rule_id, "repository-unavailable")

    def test_linked_source_directory_cannot_report_complete_coverage(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            repo = root / "repo"
            repo.mkdir()
            source = root / "source"
            source.mkdir()
            (source / "ops.py").write_text("ACTIONS_ID_TOKEN_REQUEST_TOKEN\n", encoding="utf-8")
            (repo / "source").symlink_to(source, target_is_directory=True)
            result = scan_odoo_ownership_boundaries(
                workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "unavailable")

    def test_nonregular_source_file_cannot_hang_the_scan(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            (root / "repo").mkdir()
            os.mkfifo(root / "repo" / "ops.py")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "unavailable")

    def test_local_binary_metadata_does_not_block_source_scan(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            repo = root / "repo"
            repo.mkdir()
            for name in (".DS_Store", ".coverage"):
                (repo / name).write_bytes(b"\xff\x00")
            (repo / "build.py").write_text("print('build')\n", encoding="utf-8")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.scanned_file_count, 1)

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

    def test_cli_supplies_only_explicit_scoped_path_exceptions(self) -> None:
        with TemporaryDirectory() as workspace:
            repo = Path(workspace) / "tool"
            source = repo / "local.py"
            repo.mkdir()
            source.write_text("print('shared production restore')\n", encoding="utf-8")
            runner = CliRunner()
            command = [
                "odoo-ownership",
                "check",
                "--repository",
                str(repo),
                "devkit",
                "--format",
                "json",
            ]
            unexcepted = runner.invoke(main, command)
            result = runner.invoke(main, [*command, "--allow-path", str(repo), "local.py"])
            unknown = runner.invoke(main, [*command, "--allow-path", "other-root", "local.py"])
        self.assertNotEqual(unexcepted.exit_code, 0)
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["coverage"][0]["omitted_file_count"], 1)
        self.assertEqual(payload["coverage"][0]["scanned_file_count"], 0)
        self.assertNotEqual(unknown.exit_code, 0)

    def test_local_environment_and_generated_metadata_are_outside_source_coverage(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".env").write_text("DOKPLOY_TOKEN=fixture-value\n", encoding="utf-8")
            (repo / "._ops.py").write_bytes(b"\xff\x00")
            for environment in ("venv", ".tox"):
                binary = repo / environment / "bin" / "python"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"\xff\x00")
            (repo / "ops.py").write_text("ACTIONS_ID_TOKEN_REQUEST_TOKEN\n", encoding="utf-8")
            result = scan_odoo_ownership_boundaries(
                workspace_root=root, repo_policies=(OdooOwnershipRepoPolicy("repo", "tenant"),)
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.coverage[0].status, "complete")
        self.assertEqual(result.scanned_file_count, 1)
        self.assertEqual({finding.path for finding in result.findings}, {"ops.py"})
        self.assertNotIn("fixture-value", str(result.to_json_payload()))
