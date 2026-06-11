import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.config_authority_audit import build_config_authority_audit
from control_plane.config_authority_audit import render_config_authority_markdown
from control_plane.config_authority_audit import _allow_reason


CLI_MAIN = cast(Command, main)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "launchplane-test@example.invalid")
    _git(root, "config", "user.name", "Launchplane Test")


def _commit_all(root: Path) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def _checkout_branch(root: Path, branch_name: str) -> None:
    _git(root, "checkout", "-b", branch_name)


def _findings(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", payload["findings"])


def _gaps(payload: dict[str, object]) -> list[dict[str, object]]:
    coverage = cast("dict[str, object]", payload["coverage"])
    return cast("list[dict[str, object]]", coverage["gaps"])


class ConfigAuthorityAuditTest(unittest.TestCase):
    def test_python_repo_policy_default_is_reported_and_redacted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "defaults.py"
            source.parent.mkdir()
            source.write_text(
                'DEFAULT_REPOSITORIES = ("cbusillo/private-product",)\n'
                'API_TOKEN = "secret-value"\n',
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings = _findings(payload)
        repository_finding = next(
            finding for finding in findings if finding["key"] == "DEFAULT_REPOSITORIES"
        )
        secret_finding = next(finding for finding in findings if finding["key"] == "API_TOKEN")
        self.assertEqual(repository_finding["classification"], "needs_classification")
        self.assertEqual(repository_finding["rule_id"], "repository_authority")
        self.assertEqual(repository_finding["evidence"], "<redacted-repository>")
        self.assertEqual(secret_finding["evidence"], "<redacted-secret-shaped-value>")
        self.assertNotIn("private-product", json.dumps(payload))
        self.assertNotIn("secret-value", json.dumps(payload))

    def test_python_dataclass_policy_catalog_is_reported_by_shape(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "catalog.py"
            source.parent.mkdir()
            source.write_text(
                "from dataclasses import dataclass\n\n"
                "@dataclass(frozen=True)\n"
                "class RepoPolicy:\n"
                "    repository: str\n"
                "    family: str\n"
                "    allowed_path_globs: tuple[str, ...] = ()\n\n"
                "DEFAULT_REPO_POLICIES = (\n"
                "    RepoPolicy('tenant-primary', 'tenant'),\n"
                "    RepoPolicy(repository='image-builder', family='image'),\n"
                ")\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings = _findings(payload)
        keys = {str(finding["key"]) for finding in findings}
        self.assertIn("DEFAULT_REPO_POLICIES[0].RepoPolicy.repository", keys)
        self.assertIn("DEFAULT_REPO_POLICIES[1].RepoPolicy.repository", keys)
        repository_findings = [
            finding for finding in findings if str(finding["key"]).endswith(".repository")
        ]
        family_findings = [
            finding for finding in findings if str(finding["key"]).endswith(".family")
        ]
        self.assertTrue(repository_findings)
        self.assertTrue(
            all(finding["rule_id"] == "repository_authority" for finding in repository_findings)
        )
        self.assertTrue(family_findings)
        self.assertTrue(
            all(finding["rule_id"] == "runtime_config_authority" for finding in family_findings)
        )
        self.assertTrue(
            all(finding["evidence"] == "<redacted-repository>" for finding in repository_findings)
        )
        self.assertNotIn("tenant-primary", json.dumps(payload))
        self.assertNotIn("image-builder", json.dumps(payload))

    def test_module_qualified_dataclass_policy_catalog_is_reported_by_shape(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "catalog.py"
            source.parent.mkdir()
            source.write_text(
                "from dataclasses import dataclass\n\n"
                "@dataclass(frozen=True)\n"
                "class RepoPolicy:\n"
                "    repository: str\n"
                "    family: str\n\n"
                "class policies:\n"
                "    RepoPolicy = RepoPolicy\n\n"
                "DEFAULT_REPO_POLICIES = (\n"
                "    policies.RepoPolicy('tenant-qualified', 'tenant'),\n"
                ")\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        keys = {str(finding["key"]) for finding in _findings(payload)}
        self.assertIn(
            "DEFAULT_REPO_POLICIES[0].policies.RepoPolicy.repository",
            keys,
        )
        self.assertNotIn("tenant-qualified", json.dumps(payload))

    def test_known_odoo_repo_policy_catalog_is_reported_without_name_catalog(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "odoo_ownership_checks.py"
            source.parent.mkdir()
            source.write_text(
                "from dataclasses import dataclass\n"
                "from typing import Literal\n\n"
                "OdooRepoFamily = Literal['tenant', 'image', 'shared_addons', 'devkit']\n\n"
                "@dataclass(frozen=True)\n"
                "class OdooOwnershipRepoPolicy:\n"
                "    repository: str\n"
                "    family: OdooRepoFamily\n"
                "    allowed_path_globs: tuple[str, ...] = ()\n\n"
                "DEFAULT_ODOO_REPO_POLICIES: tuple[OdooOwnershipRepoPolicy, ...] = (\n"
                "    OdooOwnershipRepoPolicy('odoo-tenant-cm', 'tenant'),\n"
                "    OdooOwnershipRepoPolicy(\n"
                "        'odoo-devkit',\n"
                "        'devkit',\n"
                "        allowed_path_globs=('odoo_devkit/dokploy_api.py',),\n"
                "    ),\n"
                ")\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        keys = {str(finding["key"]) for finding in _findings(payload)}
        self.assertIn(
            "DEFAULT_ODOO_REPO_POLICIES[0].OdooOwnershipRepoPolicy.repository",
            keys,
        )
        self.assertIn(
            "DEFAULT_ODOO_REPO_POLICIES[1].OdooOwnershipRepoPolicy.repository",
            keys,
        )
        path_glob_findings = [
            finding for finding in _findings(payload) if "allowed_path_globs" in str(finding["key"])
        ]
        self.assertTrue(path_glob_findings)
        self.assertTrue(
            all(finding["allow_reason"] == "schema_only" for finding in path_glob_findings)
        )
        self.assertNotIn("odoo-tenant-cm", json.dumps(payload))
        self.assertNotIn("odoo-devkit", json.dumps(payload))

    def test_docs_tests_and_bootstrap_have_explicit_allow_reasons(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "docs").mkdir()
            (root / "tests").mkdir()
            (root / "docs" / "example.md").write_text(
                "PRODUCT_REPOSITORY=cbusillo/docs-example\n", encoding="utf-8"
            )
            (root / "tests" / "fixture.env").write_text(
                "PRODUCT_DOMAIN=https://fixture.example.test\n", encoding="utf-8"
            )
            (root / "service.env").write_text(
                "LAUNCHPLANE_DATABASE_URL=postgres://launchplane-db\n", encoding="utf-8"
            )
            (root / "addons" / "product_feature").mkdir(parents=True)
            (root / "addons" / "product_feature" / "models.py").write_text(
                'PRODUCT_MODEL = "product.product"\n', encoding="utf-8"
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        reasons = {finding["allow_reason"] for finding in _findings(payload)}
        self.assertIn("docs_example", reasons)
        self.assertIn("test_fixture", reasons)
        self.assertIn("launchplane_self_bootstrap", reasons)
        self.assertIn("product_owned_addon", reasons)
        self.assertNotIn("", reasons)

    def test_import_material_is_not_allowed_runtime_authority(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            import_material = root / "import-material" / "seed.json"
            import_material.parent.mkdir()
            import_material.write_text(
                '{"repository":"cbusillo/import-example"}\n', encoding="utf-8"
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings = _findings(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "needs_classification")
        self.assertEqual(findings[0]["allow_reason"], "")

    def test_dependency_lockfile_is_reported_as_coverage_gap_not_findings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "package-lock.json").write_text(
                '{"packages":{"node_modules/example":{"resolved":"https://registry.example.test/example.tgz"}}}\n',
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        self.assertEqual(_findings(payload), [])
        self.assertEqual(_gaps(payload)[0]["reason"], "skipped_dependency_manifest")

    def test_include_untracked_does_not_include_ignored_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
            (root / "tracked.env").write_text("PRODUCT_CONTEXT=tracked\n", encoding="utf-8")
            _commit_all(root)
            (root / "new.env").write_text("PRODUCT_CONTEXT=review\n", encoding="utf-8")
            (root / "ignored.env").write_text("PRODUCT_CONTEXT=ignored\n", encoding="utf-8")

            untracked_payload = build_config_authority_audit(
                control_plane_root=root,
                include_untracked=True,
            )
            ignored_payload = build_config_authority_audit(
                control_plane_root=root,
                include_ignored=True,
            )

        untracked_paths = {finding["path"] for finding in _findings(untracked_payload)}
        ignored_paths = {finding["path"] for finding in _findings(ignored_payload)}
        self.assertIn("tracked.env", untracked_paths)
        self.assertIn("new.env", untracked_paths)
        self.assertNotIn("ignored.env", untracked_paths)
        self.assertIn("ignored.env", ignored_paths)

    def test_changed_files_gate_scans_committed_branch_diff(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "tracked.env").write_text("PRODUCT_CONTEXT=main\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-audit")
            (root / "tracked.env").write_text("PRODUCT_CONTEXT=feature\n", encoding="utf-8")
            (root / "new.env").write_text(
                "PRODUCT_REPOSITORY=cbusillo/new-product\n", encoding="utf-8"
            )
            _commit_all(root)

            payload = build_config_authority_audit(
                control_plane_root=root,
                mode="changed-files-gate",
            )

        finding_paths = {finding["path"] for finding in _findings(payload)}
        self.assertEqual(finding_paths, {"new.env", "tracked.env"})
        coverage = cast("dict[str, object]", payload["coverage"])
        self.assertEqual(coverage["source_file_count"], 2)

    def test_click_option_metadata_is_reported_as_operator_input(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "cli_options.py"
            source.parent.mkdir()
            source.write_text(
                "import click\n\n"
                "@click.option('--product', default='')\n"
                "@click.option('--apply', is_flag=True, required=True)\n"
                "def command(product: str, apply: bool) -> None:\n"
                "    return None\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        by_key = {finding["key"]: finding for finding in _findings(payload)}
        self.assertIn("--product.default", by_key)
        self.assertIn("--apply.required", by_key)
        self.assertEqual(
            by_key["--product.default"]["allow_reason"],
            "operator_supplied_runtime_input",
        )
        self.assertEqual(
            by_key["--apply.required"]["allow_reason"],
            "operator_supplied_runtime_input",
        )

    def test_repo_metadata_ergonomics_are_allowed_but_topology_is_not(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            metadata = root / ".github" / "github.json"
            metadata.parent.mkdir()
            metadata.write_text(
                json.dumps(
                    {
                        "defaultBranch": "main",
                        "docs": {"operations": "docs/operations.md"},
                        "importantWorkflows": ["CI"],
                        "pullRequests": {"preferredMergeMethod": "merge"},
                        "qualityGate": {
                            "security": {"secretScan": "docker run ghcr.io/example/gitleaks:latest"}
                        },
                        "githubSettings": {"actions": {"enabled": True}},
                        "relatedRepos": ["example/example-product"],
                        "healthUrls": [
                            {"name": "testing", "url": "https://product.example.test/health"}
                        ],
                        "product": {
                            "name": "real-product",
                            "domain": "product.example.test",
                            "owner": "Real Operator",
                        },
                        "launchplane": {
                            "lanes": {
                                "testing": {
                                    "url": "https://product.example.test",
                                    "deployRoute": "/v1/drivers/generic-web/deploy",
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        by_key = {finding["key"]: finding for finding in _findings(payload)}
        self.assertEqual(
            by_key["defaultBranch"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["docs.operations"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["importantWorkflows[0]"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["pullRequests.preferredMergeMethod"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["qualityGate.security.secretScan"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["githubSettings.actions.enabled"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(
            by_key["relatedRepos[0]"]["allow_reason"],
            "repo_metadata_ergonomics",
        )
        self.assertEqual(by_key["healthUrls[0].url"]["allow_reason"], "")
        self.assertEqual(by_key["product.name"]["allow_reason"], "")
        self.assertEqual(by_key["product.name"]["evidence"], "<redacted-runtime-identity>")
        self.assertEqual(by_key["product.owner"]["evidence"], "<redacted-runtime-identity>")
        self.assertEqual(by_key["launchplane.lanes.testing.url"]["allow_reason"], "")
        self.assertEqual(by_key["launchplane.lanes.testing.deployRoute"]["allow_reason"], "")

    def test_hashes_are_stable_for_same_inputs_and_findings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "config.json").write_text(
                '{"repository":"cbusillo/stable-product"}\n', encoding="utf-8"
            )
            _commit_all(root)

            first = build_config_authority_audit(control_plane_root=root)
            second = build_config_authority_audit(control_plane_root=root)

        self.assertEqual(first["hashes"], second["hashes"])

    def test_allow_reason_constants_are_schema_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "config_authority_audit.py"
            source.parent.mkdir()
            source.write_text(
                "ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT = "
                '"operator_supplied_runtime_input"\n',
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings = _findings(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["allow_reason"], "schema_only")
        self.assertEqual(findings[0]["classification"], "allowed")

    def test_python_literal_with_tuple_keys_does_not_crash_scan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            source = root / "control_plane" / "tuple_keys.py"
            source.parent.mkdir()
            source.write_text(
                "DEFAULT_TARGET_MATRIX = {\n"
                "    ('tenant', 'prod'): {'repository': 'tenant-prod'},\n"
                "}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        self.assertGreaterEqual(len(_findings(payload)), 1)
        self.assertNotIn("tenant-prod", json.dumps(payload))

    def test_dirty_worktree_tracks_head_index_and_worktree_hashes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            tracked_file = root / "config.json"
            tracked_file.write_text(
                '{"repository":"cbusillo/original-product"}\n', encoding="utf-8"
            )
            _commit_all(root)
            tracked_file.write_text('{"repository":"cbusillo/changed-product"}\n', encoding="utf-8")

            payload = build_config_authority_audit(control_plane_root=root)

        source_file = cast("list[dict[str, object]]", payload["source_files"])[0]
        self.assertEqual(source_file["git_status"], "M")
        self.assertNotEqual(source_file["head_blob_sha"], "")
        self.assertEqual(source_file["index_blob_sha"], source_file["head_blob_sha"])
        self.assertNotEqual(source_file["worktree_sha256"], source_file["head_blob_sha"])

    def test_coverage_gaps_capture_binary_large_and_yaml_parser_limitation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "binary.txt").write_bytes(b"abc\0def")
            (root / "large.txt").write_text("x" * 1_000_001, encoding="utf-8")
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "deploy.yml").write_text(
                "name: Deploy\nwith:\n  product_domain: https://product.example.test\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        gap_reasons = {gap["reason"] for gap in _gaps(payload)}
        self.assertIn("skipped_binary_file", gap_reasons)
        self.assertIn("skipped_large_file", gap_reasons)
        self.assertIn("parser_limitation", gap_reasons)
        self.assertTrue(
            any(str(finding["path"]).endswith("deploy.yml") for finding in _findings(payload))
        )

    def test_workflow_line_scan_ignores_display_text_but_keeps_defaults(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "preview.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Preview product deployment\n"
                "jobs:\n"
                "  preview:\n"
                "    steps:\n"
                "      - name: Fetch Launchplane protected artifact inventory\n"
                "        description: Store cbusillo/example-repo in Launchplane\n"
                "        uses: cbusillo/launchplane/.github/actions/launchplane-request@main\n"
                "      - run: echo https://display.example.test\n"
                "        with:\n"
                "          product: concrete-product\n"
                "          route-path: /v1/drivers/preview\n"
                "          public-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "          client-secret: ${{ secrets.LAUNCHPLANE_CLIENT_SECRET }}\n"
                "          product-input: ${{ inputs.product }}\n"
                "          mixed-url: https://${{ vars.LAUNCHPLANE_DOMAIN }}/health\n"
                "          fallback-product: ${{ inputs.product || 'launchplane' }}\n"
                "          input-fallback-product: ${{ inputs.product || 'launchplane' }}\n"
                "          LAUNCHPLANE_PRODUCT: ${{ inputs.product || 'launchplane' }}\n"
                "          launchplane-product: ${{ inputs.product }}\n"
                "          LAUNCHPLANE_URL: https://${{ vars.LAUNCHPLANE_DOMAIN }}\n"
                "          launchplane-url: ${{ vars.LAUNCHPLANE_URL }}\n"
                "          route-path-fallback: ${{ inputs.route_path || '/v1/drivers/preview' }}\n"
                "          target-context: launchplane\n"
                "          literal-product: concrete-product\n"
                "          literal-domain: https://runtime.example.test\n"
                "          default: launchplane\n"
                "          folded-secret: >-\n"
                "            ${{ secrets.FOLDED_SECRET }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        keys = [finding["key"] for finding in _findings(payload)]
        self.assertIn("product", keys)
        self.assertNotIn("name", keys)
        self.assertNotIn("description", keys)
        self.assertNotIn("uses", keys)
        self.assertNotIn("run", keys)
        self.assertNotIn("folded-secret", keys)
        route_path = next(
            finding for finding in _findings(payload) if finding["key"] == "route-path"
        )
        self.assertEqual(route_path["allow_reason"], "thin_connector_input")
        for key in ("public-url", "client-secret", "product-input"):
            finding = next(finding for finding in _findings(payload) if finding["key"] == key)
            self.assertEqual(finding["allow_reason"], "thin_connector_input")
        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        for key in (
            "mixed-url",
            "fallback-product",
            "input-fallback-product",
            "LAUNCHPLANE_PRODUCT",
            "launchplane-product",
            "LAUNCHPLANE_URL",
            "launchplane-url",
            "route-path-fallback",
            "target-context",
            "literal-product",
            "literal-domain",
        ):
            finding = findings_by_key[key]
            self.assertEqual(finding["classification"], "needs_classification")

    def test_workflow_route_path_forwarding_is_thin_connector_input(self) -> None:
        for value in (
            "${{ steps.route.outputs.route_path }}",
            "${{ steps.resolve-route.outputs.api_path }}",
            "${{ inputs.route_path }}",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/tracked-target-logs.yml",
                        key="route-path",
                        value=value,
                    ),
                    "thin_connector_input",
                )

    def test_workflow_route_path_forwarding_is_scanned_and_allowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "tracked-target-logs.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Tracked target logs\n"
                "jobs:\n"
                "  logs:\n"
                "    steps:\n"
                "      - id: route\n"
                "        run: echo route_path=/v1/logs >> $GITHUB_OUTPUT\n"
                "      - with:\n"
                "          route-path: ${{ steps.route.outputs.route_path }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        route_path = next(
            finding for finding in _findings(payload) if finding["key"] == "route-path"
        )
        self.assertEqual(route_path["classification"], "allowed")
        self.assertEqual(route_path["allow_reason"], "thin_connector_input")

    def test_workflow_operator_input_forwarding_is_scanned_and_allowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "target.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Target\n"
                "jobs:\n"
                "  setup:\n"
                "    env:\n"
                "      TARGET_ID: ${{ inputs.target_id }}\n"
                "    steps:\n"
                "      - with:\n"
                "          target_id: $target_id,\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        self.assertEqual(
            findings_by_key["TARGET_ID"]["allow_reason"],
            "operator_supplied_runtime_input",
        )
        self.assertEqual(
            findings_by_key["target_id"]["allow_reason"],
            "operator_supplied_runtime_input",
        )

    def test_workflow_route_path_fallbacks_stay_unclassified(self) -> None:
        for value in (
            "${{ inputs.route_path || '/v1/drivers/preview' }}",
            "${{ steps.route.outputs.route_path || '/v1/logs' }}",
            "/v1/${{ inputs.route_path }}",
            "${{ vars.ROUTE_PATH }}",
            "${{ env.ROUTE_PATH }}",
            "${{ secrets.ROUTE_PATH }}",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/tracked-target-logs.yml",
                        key="route-path",
                        value=value,
                    ),
                    "",
                )

    def test_workflow_operator_inputs_and_mechanics_are_classified(self) -> None:
        operator_inputs = (
            ("context", "${{ inputs.context }}"),
            ("target_id", "${{ inputs.target_id }}"),
            ("environment_name", "${{ inputs.environment_name }}"),
            ("compose_path", "${{ inputs.compose_path }}"),
            ("healthcheck_path", "${{ inputs.healthcheck_path }}"),
            ("repository", "${{ inputs.repository }}"),
            ("base_branch", "${{ inputs.base_branch }}"),
            ("source_git_ref", "${{ inputs.source_git_ref }}"),
        )
        for key, value in operator_inputs:
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/dokploy-target-setup.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )

        forwarded_variables = (
            ("context", "$context"),
            ("target_id", "$target_id"),
            ("environment_name", "$environment_name,"),
            ("repository", "$repository"),
            ("base_branch", "$base_branch,"),
        )
        for key, value in forwarded_variables:
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/dokploy-target-setup.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )

        mechanics = (
            ("id-token", "write"),
            ("group", "dokploy-target-setup-${{ inputs.context }}-${{ inputs.instance }}"),
            ("path", "dokploy-target-setup.json"),
        )
        for key, value in mechanics:
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/dokploy-target-setup.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )

    def test_workflow_operator_input_allow_reasons_stay_narrow(self) -> None:
        for key, value in (
            ("target_id", "dokploy-real-target"),
            ("target_id", "${{ vars.TARGET_ID }}"),
            ("target_id", "$provider_target_id"),
            ("repository", "cbusillo/odoo-devkit"),
            ("repository", "${{ inputs.repository || vars.DEFAULT_REPOSITORY }}"),
            ("base_branch", "main"),
            ("base_branch", "${{ inputs.base_branch || 'main' }}"),
            ("healthcheck_path", "${{ vars.HEALTHCHECK_PATH }}"),
            ("group", "dokploy-target-setup-${{ vars.CONTEXT }}"),
            ("path", "provider-target/live.json"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/dokploy-target-setup.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_workflow_launchplane_public_url_reference_is_self_bootstrap(self) -> None:
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/merge-train-runner.yml",
                key="LAUNCHPLANE_URL",
                value="${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
            ),
            "launchplane_self_bootstrap",
        )

        for key, value in (
            ("LAUNCHPLANE_URL", "${{ vars.OTHER_URL }}"),
            ("LAUNCHPLANE_URL", "https://launchplane.example.invalid"),
            ("launchplane-url", "${{ vars.LAUNCHPLANE_PUBLIC_URL }}"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/merge-train-runner.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_cli_outputs_json_and_markdown(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            (root / "config.json").write_text(
                '{"repository":"cbusillo/cli-product"}\n', encoding="utf-8"
            )
            _commit_all(root)

            runner = CliRunner()
            json_result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                ],
            )
            markdown_result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--format",
                    "markdown",
                ],
            )
            changed_files_result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                ],
            )

        self.assertEqual(json_result.exit_code, 0, json_result.output)
        self.assertEqual(markdown_result.exit_code, 0, markdown_result.output)
        self.assertEqual(changed_files_result.exit_code, 0, changed_files_result.output)
        self.assertEqual(json.loads(json_result.output)["mode"], "full-audit")
        self.assertEqual(
            json.loads(changed_files_result.output)["mode"],
            "changed-files-gate",
        )
        self.assertIn("# Config Authority Audit", markdown_result.output)

    def test_markdown_renderer_handles_empty_report(self) -> None:
        markdown = render_config_authority_markdown(
            {
                "mode": "full-audit",
                "coverage": {
                    "source_file_count": 0,
                    "finding_count": 0,
                    "coverage_gap_count": 0,
                    "gaps": [],
                },
                "hashes": {"input_set_hash": "abc", "finding_set_hash": "def"},
                "findings": [],
            }
        )

        self.assertIn("No findings.", markdown)
        self.assertIn("No coverage gaps.", markdown)


if __name__ == "__main__":
    unittest.main()
