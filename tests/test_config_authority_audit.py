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
from control_plane.config_authority_audit import evaluate_config_authority_gate
from control_plane.config_authority_audit import render_config_authority_markdown
from control_plane.config_authority_audit import _allow_reason
from control_plane.config_authority_audit import _workflow_path_key_values


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
    def test_merge_train_runner_uses_policy_targets_for_scheduled_authority(self) -> None:
        workflow_text = Path(".github/workflows/merge-train-runner.yml").read_text(encoding="utf-8")

        self.assertIn("/v1/work-graph/merge-train/policy-targets", workflow_text)
        self.assertNotIn("LAUNCHPLANE_MERGE_TRAIN_REPOSITORY", workflow_text)
        self.assertNotIn("LAUNCHPLANE_MERGE_TRAIN_BASE_BRANCH", workflow_text)
        self.assertNotIn("LAUNCHPLANE_MERGE_TRAIN_MUTATE", workflow_text)
        self.assertNotIn("LAUNCHPLANE_MERGE_TRAIN_RUNNER_MODE", workflow_text)

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

    def test_artifact_publish_schema_constants_are_schema_only(self) -> None:
        for key, value in (
            ("DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY", "ODOO_DEVKIT_RUNTIME_ENVIRONMENT_JSON"),
            (
                "PUBLISH_RUNTIME_ENVIRONMENT_KEYS",
                (
                    "ODOO_VERSION",
                    "ODOO_BASE_RUNTIME_IMAGE",
                    "ODOO_BASE_DEVTOOLS_IMAGE",
                    "ODOO_ADDON_REPOSITORIES",
                    "OPENUPGRADE_ADDON_REPOSITORY",
                    "OPENUPGRADELIB_INSTALL_SPEC",
                    "ODOO_PYTHON_SYNC_SKIP_ADDONS",
                ),
            ),
            ("PUBLISH_RUNTIME_ENVIRONMENT_KEYS[0]", "ODOO_VERSION"),
            (
                "PUBLISH_DEPENDENCY_REPOSITORY_KEYS",
                {
                    "devkit_repository": "ODOO_DEVKIT_REPOSITORY",
                    "shared_addons_repository": "ODOO_SHARED_ADDONS_REPOSITORY",
                },
            ),
            ("PUBLISH_DEPENDENCY_REPOSITORY_KEYS.devkit_repository", "ODOO_DEVKIT_REPOSITORY"),
            ("instance", "testing"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path="control_plane/workflows/odoo_artifact_publish.py",
                        key=key,
                        value=value,
                    ),
                    "schema_only",
                )
                self.assertEqual(
                    _allow_reason(
                        path="control_plane/workflows/other_publish.py",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for key, value in (
            ("DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY", "REAL_PRODUCT_ENVIRONMENT_JSON"),
            ("PUBLISH_RUNTIME_ENVIRONMENT_KEYS", "ODOO_VERSION"),
            ("PUBLISH_RUNTIME_ENVIRONMENT_KEYS[0]", "REAL_PRODUCT_DOMAIN"),
            ("PUBLISH_DEPENDENCY_REPOSITORY_KEYS", "ODOO_DEVKIT_REPOSITORY"),
            ("PUBLISH_DEPENDENCY_REPOSITORY_KEYS.devkit_repository", "real-owner/real-repo"),
            ("instance", "prod"),
        ):
            with self.subTest(key=key, wrong_value=value):
                self.assertEqual(
                    _allow_reason(
                        path="control_plane/workflows/odoo_artifact_publish.py",
                        key=key,
                        value=value,
                    ),
                    "",
                )

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
                "          event-product-input: ${{ github.event.inputs.product }}\n"
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
        route_path = next(
            finding for finding in _findings(payload) if finding["key"] == "route-path"
        )
        self.assertEqual(route_path["allow_reason"], "thin_connector_input")
        public_url = next(
            finding for finding in _findings(payload) if finding["key"] == "public-url"
        )
        self.assertEqual(public_url["allow_reason"], "operator_supplied_runtime_input")
        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        for key in ("product-input", "event-product-input"):
            finding = findings_by_key[key]
            self.assertEqual(finding["classification"], "needs_classification")
            self.assertEqual(finding["allow_reason"], "")
        for key in (
            "client-secret",
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
            "folded-secret",
        ):
            finding = findings_by_key[key]
            self.assertEqual(finding["classification"], "needs_classification")

    def test_workflow_input_defaults_preserve_input_context(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "provider.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                '"on":\n'
                "  workflow_dispatch:\n"
                '    "inputs":\n'
                "      provider_id:\n"
                "        description: Provider id\n"
                "        required: false\n"
                "        default: dokploy\n"
                "        type: string\n"
                "      base_branch:\n"
                "        description: Base branch\n"
                "        required: false\n"
                "        default: main\n"
                "        type: string\n"
                "  workflow_call:\n"
                "    inputs: # reusable workflow inputs\n"
                "      instance:\n"
                "        required: false\n"
                "        default: testing\n"
                "        type: string\n"
                "      runtime_port:\n"
                "        required: false\n"
                '        default: "8069"\n'
                "        type: string\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        self.assertEqual(
            findings_by_key["inputs.provider_id.default"]["classification"],
            "needs_classification",
        )
        self.assertEqual(
            findings_by_key["inputs.provider_id.default"]["rule_id"],
            "provider_target_authority",
        )
        self.assertEqual(
            findings_by_key["inputs.base_branch.default"]["classification"],
            "needs_classification",
        )
        self.assertEqual(
            findings_by_key["inputs.base_branch.default"]["rule_id"],
            "runtime_config_authority",
        )
        self.assertEqual(
            findings_by_key["inputs.instance.default"]["classification"],
            "needs_classification",
        )
        self.assertEqual(
            findings_by_key["inputs.runtime_port.default"]["classification"],
            "needs_classification",
        )

    def test_explicit_directory_paths_are_scanned(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "provider.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                '"on":\n'
                "  workflow_dispatch:\n"
                "    inputs:\n"
                "      provider_id:\n"
                "        default: dokploy\n"
                "        type: string\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(
                control_plane_root=root,
                paths=(Path(".github/workflows"),),
            )

        self.assertTrue(
            any(finding["key"] == "inputs.provider_id.default" for finding in _findings(payload))
        )

    def test_workflow_input_mechanic_defaults_are_path_scoped(self) -> None:
        mechanic_defaults = (
            (
                ".github/workflows/public-ingress-monitor.yml",
                "inputs.timeout_seconds.default",
                "10",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.action.default",
                "prune_docker_cache",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.minimum_free_disk_bytes.default",
                "0",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.prune_until.default",
                "168h",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.timeout_seconds.default",
                "300",
            ),
        )
        for path, key, value in mechanic_defaults:
            with self.subTest(path=path, key=key, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )

        for path, key, value in (
            (
                ".github/workflows/generic-workflow.yml",
                "inputs.timeout_seconds.default",
                "10",
            ),
            (
                ".github/workflows/public-ingress-monitor.yml",
                "inputs.runtime_port.default",
                "10",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.action.default",
                "remove_buildkit_state_volumes",
            ),
            (
                ".github/workflows/runner-host-hygiene.yml",
                "inputs.timeout_seconds.default",
                "600",
            ),
            (
                ".github/workflows/provider-target-operations.yml",
                "inputs.provider_id.default",
                "auto",
            ),
            (
                ".github/workflows/provider-target-operations.yml",
                "inputs.context.default",
                "single",
            ),
        ):
            with self.subTest(path=path, key=key, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "",
                )

    def test_workflow_runs_on_list_items_are_scanned(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "runner.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "jobs:\n"
                "  test:\n"
                "    runs-on:\n"
                "      - self-hosted\n"
                "      - ubuntu-latest\n"
                "      - launchplane\n"
                "      - chris-testing-ops-gate\n"
                "      - ${{ vars.LAUNCHPLANE_RUNNER_LABEL }}\n"
                "      - ${{ vars.OTHER_RUNNER_LABEL }}\n"
                "    steps:\n"
                "      - run: echo ok\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        lane_findings = [
            finding
            for finding in _findings(payload)
            if finding["key"] == "runs-on" and finding["rule_id"] == "runtime_config_authority"
        ]
        findings_by_evidence = {finding["evidence"]: finding for finding in lane_findings}
        for allowed_value in (
            "self-hosted",
            "ubuntu-latest",
            "${{ vars.LAUNCHPLANE_RUNNER_LABEL }}",
        ):
            self.assertEqual(findings_by_evidence[allowed_value]["classification"], "allowed")
        for blocked_value in (
            "launchplane",
            "chris-testing-ops-gate",
            "${{ vars.OTHER_RUNNER_LABEL }}",
        ):
            self.assertEqual(
                findings_by_evidence[blocked_value]["classification"],
                "needs_classification",
            )

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

    def test_workflow_block_scalar_runtime_values_are_scanned(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "merge-train-runner.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Merge train\n"
                "jobs:\n"
                "  run:\n"
                "    env:\n"
                "      MERGE_TRAIN_REPOSITORY: >-\n"
                "        ${{ inputs.repository || vars.LAUNCHPLANE_MERGE_TRAIN_REPOSITORY }}\n"
                "      MERGE_TRAIN_BASE_BRANCH: >-\n"
                "        ${{\n"
                "          github.event_name == 'schedule' &&\n"
                "          vars.LAUNCHPLANE_MERGE_TRAIN_BASE_BRANCH ||\n"
                "          inputs.base_branch\n"
                "        }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        for key in ("MERGE_TRAIN_REPOSITORY", "MERGE_TRAIN_BASE_BRANCH"):
            with self.subTest(key=key):
                finding = findings_by_key[key]
                self.assertEqual(finding["classification"], "needs_classification")
                self.assertEqual(finding["allow_reason"], "")

    def test_workflow_run_blocks_are_not_parsed_as_yaml_mappings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "merge-train-runner.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Merge train\n"
                "jobs:\n"
                "  run:\n"
                "    steps:\n"
                "      - name: Forward operator payload\n"
                "        run: |\n"
                "          payload = {\n"
                '              "repository": env.REPOSITORY,\n'
                '              "base_branch": env.BASE_BRANCH,\n'
                "          }\n"
                "          print(payload)\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        keys = {finding["key"] for finding in _findings(payload)}
        self.assertNotIn("repository", keys)
        self.assertNotIn("base_branch", keys)

    def test_workflow_block_scalar_payload_fields_are_scanned_individually(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "testing-deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Testing deploy\n"
                "jobs:\n"
                "  testing-deploy:\n"
                "    steps:\n"
                "      - id: lp\n"
                "        with:\n"
                "          payload-fields: |-\n"
                "            product=${{ steps.product.outputs.product }}\n"
                "            replacement.product=${{ steps.product.outputs.product }}\n"
                "            replacement.artifact_id=${{ inputs.artifact_id }}\n"
                "            replacement.source_git_ref=${{ github.event.inputs.source_git_ref }}\n"
                "            replacement.reason=${{ env.REASON }}\n"
                "            replacement.branch=main\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings_by_key: dict[str, list[dict[str, object]]] = {}
        for finding in _findings(payload):
            findings_by_key.setdefault(str(finding["key"]), []).append(finding)
        for key in (
            "payload-fields.product",
            "payload-fields.replacement.product",
            "payload-fields.replacement.artifact_id",
            "payload-fields.replacement.source_git_ref",
            "payload-fields.replacement.reason",
            "payload-fields.replacement.branch",
        ):
            self.assertIn(key, findings_by_key)
            self.assertEqual(len(findings_by_key[key]), 1)
        self.assertEqual(
            findings_by_key["payload-fields.replacement.branch"][0]["evidence"],
            "<redacted-runtime-identity>",
        )
        self.assertEqual(
            findings_by_key["payload-fields.replacement.branch"][0]["allow_reason"],
            "test_fixture",
        )

    def test_workflow_payload_field_allowlist_matches_scanned_key_prefix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "reusable-odoo-prod-promotion.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Reusable prod promotion\n"
                "on:\n"
                "  workflow_call:\n"
                "jobs:\n"
                "  promote:\n"
                "    steps:\n"
                "      - uses: cbusillo/launchplane/.github/actions/launchplane-request@main\n"
                "        with:\n"
                "          payload-fields: |-\n"
                "            run.request_id=${{ github.run_id }}-${{ github.run_attempt }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings = [
            finding
            for finding in _findings(payload)
            if finding["key"] == "payload-fields.run.request_id"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "allowed")
        self.assertEqual(findings[0]["allow_reason"], "thin_connector_input")

    def test_workflow_path_key_values_bridges_payload_field_prefix(self) -> None:
        path_values = {
            ".github/workflows/example.yml": {
                "run.request_id": frozenset(("${{ github.run_id }}",)),
                "payload-fields.run.context": frozenset(("${{ inputs.context }}",)),
            }
        }

        self.assertEqual(
            _workflow_path_key_values(
                path_values,
                path=".github/workflows/example.yml",
                key="payload-fields.run.request_id",
            ),
            frozenset(("${{ github.run_id }}",)),
        )
        self.assertEqual(
            _workflow_path_key_values(
                path_values,
                path=".github/workflows/example.yml",
                key="run.context",
            ),
            frozenset(("${{ inputs.context }}",)),
        )
        self.assertIsNone(
            _workflow_path_key_values(
                path_values,
                path=".github/workflows/other.yml",
                key="payload-fields.run.request_id",
            )
        )

    def test_workflow_operator_input_forwarding_is_scanned_and_allowed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "dokploy-target-setup.yml"
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

    def test_merge_train_runner_manual_input_aliases_are_path_scoped(self) -> None:
        for key, value in (
            ("REQUESTED_REPOSITORY", "${{ inputs.repository }}"),
            ("REQUESTED_BASE_BRANCH", "${{ inputs.base_branch }}"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/merge-train-runner.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/other.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_workflow_restricted_context_references_are_scanned_unclassified(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "generic.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Generic\n"
                "jobs:\n"
                "  scan:\n"
                "    steps:\n"
                "      - with:\n"
                "          alpha: ${{ secrets.PROD_TOKEN }}\n"
                "          beta: ${{ github.repository }}\n"
                "          gamma: ${{ github.token }}\n"
                "          delta: ${{ vars.DEFAULT_REPOSITORY }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)

        findings_by_key = {finding["key"]: finding for finding in _findings(payload)}
        for key in ("alpha", "beta", "gamma", "delta"):
            with self.subTest(key=key):
                self.assertEqual(findings_by_key[key]["classification"], "needs_classification")
                self.assertEqual(findings_by_key[key]["allow_reason"], "")

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
            ("context", "${{ github.event.inputs.context }}"),
            ("canary_key", "${{ inputs.canary_key }}"),
            ("product", "${{ inputs.product }}"),
            ("product", "${{ github.event.inputs.product }}"),
            ("target_id", "${{ inputs.target_id }}"),
            ("target_id", "${{ github.event.inputs.target_id }}"),
            ("environment_name", "${{ inputs.environment_name }}"),
            ("compose_path", "${{ inputs.compose_path }}"),
            ("edge_endpoint_key", "${{ inputs.edge_endpoint_key }}"),
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
            ("canary_key", "$canary_key"),
            ("product", "$product,"),
            ("target_id", "$target_id"),
            ("edge_endpoint_key", "$edge_endpoint_key"),
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
        generic_workflow_cases = (
            ("target_id", "dokploy-real-target"),
            ("target_id", "${{ vars.TARGET_ID }}"),
            ("target_id", "${{ inputs.product }}"),
            ("target_id", "$provider_target_id"),
            ("target_id", "$target_id"),
            ("repository", "cbusillo/odoo-devkit"),
            ("repository", "${{ inputs.repository || vars.DEFAULT_REPOSITORY }}"),
            ("token", "${{ secrets.PROD_TOKEN }}"),
            ("foo", "${{ secrets.PROD_TOKEN }}"),
            ("foo", "${{ github.repository }}"),
            ("foo", "${{ github.token }}"),
            ("foo", "${{ vars.DEFAULT_REPOSITORY }}"),
            ("base_branch", "main"),
            (
                "base_branch",
                "${{ github.event_name == 'schedule' && vars.LAUNCHPLANE_MERGE_TRAIN_BASE_BRANCH || inputs.base_branch }}",
            ),
            ("healthcheck_path", "${{ vars.HEALTHCHECK_PATH }}"),
            ("product", "launchplane"),
            ("product", "${{ vars.PRODUCT }}"),
            ("product-input", "${{ inputs.product }}"),
            ("event-product-input", "${{ github.event.inputs.product }}"),
            ("replacement.product", "launchplane"),
            ("replacement.product", "${{ vars.PRODUCT }}"),
            ("payload-fields.replacement.product", "${{ vars.PRODUCT }}"),
            ("product", "${{ github.event.inputs.context }}"),
            ("domain_names", "[real.example.test]"),
            ("group", "dokploy-target-setup-${{ vars.CONTEXT }}"),
            ("path", "provider-target/live.json"),
        )
        for key, value in generic_workflow_cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for key, value in (
            ("payload-fields.product", "${{ steps.product.outputs.product }}"),
            ("payload-fields.product", "${{ env.PRODUCT }}"),
            ("payload-fields.replacement.product", "${{ inputs.product }}"),
            ("payload-fields.replacement.product", "${{ github.event.inputs.product }}"),
            ("payload-fields.replacement.product", "${{ steps.product.outputs.product }}"),
            ("payload-fields.replacement.product", "${{ env.PRODUCT }}"),
            ("payload-fields.replacement.source_git_ref", "${{ inputs.source_git_ref }}"),
            ("payload-fields.replacement.artifact_id", "${{ steps.artifact.outputs.artifact_id }}"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )

        self.assertEqual(
            _allow_reason(
                path=".github/workflows/reusable-odoo-prod-promotion.yml",
                key="payload-fields.run.request_id",
                value="${{ github.run_id }}-${{ github.run_attempt }}",
            ),
            "thin_connector_input",
        )
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/generic-workflow.yml",
                key="payload-fields.run.request_id",
                value="${{ github.run_id }}-${{ github.run_attempt }}",
            ),
            "",
        )

        for key, value in (
            ("payload-fields.replacement.product", "${{ env.PRODUCT || vars.PRODUCT }}"),
            (
                "payload-fields.replacement.product",
                "${{ steps.product.outputs.product || vars.PRODUCT }}",
            ),
            ("payload-fields.replacement.product", "${{ vars.PRODUCT }}"),
            ("payload-fields.replacement.source_git_ref", "main"),
            ("payload-fields.replacement.artifact_id", "${{ vars.ARTIFACT_ID }}"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
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
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/ingress-route-apply.yml",
                key="launchplane-url",
                value="${{ env.LAUNCHPLANE_URL }}",
            ),
            "launchplane_self_bootstrap",
        )
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/reusable-odoo-artifact-publish.yml",
                key="launchplane-url",
                value="${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",
            ),
            "launchplane_self_bootstrap",
        )
        for path, key in (
            (".github/workflows/odoo-driver-route-smoke.yml", "LAUNCHPLANE_URL"),
            (".github/workflows/reusable-odoo-post-deploy.yml", "launchplane-url"),
            (".github/workflows/reusable-odoo-prod-promotion.yml", "launchplane-url"),
            (".github/workflows/reusable-odoo-prod-rollback.yml", "launchplane-url"),
            (".github/workflows/reusable-odoo-testing-deploy.yml", "launchplane-url"),
            (".github/workflows/reusable-odoo-testing-deploy.yml", "LAUNCHPLANE_URL"),
            (
                ".github/workflows/reusable-generic-web-preview-lifecycle.yml",
                "launchplane-url",
            ),
            (
                ".github/workflows/reusable-generic-web-preview-verification.yml",
                "launchplane-url",
            ),
        ):
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(
                        path=path,
                        key=key,
                        value="${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",
                    ),
                    "launchplane_self_bootstrap",
                )

        self.assertEqual(
            _allow_reason(
                path=".github/workflows/generic-workflow.yml",
                key="LAUNCHPLANE_URL",
                value="${{ inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL }}",
            ),
            "",
        )

        for key, value in (
            ("LAUNCHPLANE_URL", "${{ vars.OTHER_URL }}"),
            ("LAUNCHPLANE_URL", "https://launchplane.example.invalid"),
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

        self.assertEqual(
            _allow_reason(
                path=".github/workflows/merge-train-runner.yml",
                key="launchplane-url",
                value="${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
            ),
            "operator_supplied_runtime_input",
        )

    def test_workflow_connector_findings_are_classified_narrowly(self) -> None:
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/provider-target-operations.yml",
                key="product",
                value='"launchplane"',
            ),
            "launchplane_self_bootstrap",
        )
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/generic-workflow.yml",
                key="product",
                value='"launchplane"',
            ),
            "",
        )

        service_env_payload = (
            ("GHCR_USERNAME", "${{ secrets.GHCR_USERNAME }}"),
            ("LAUNCHPLANE_GITHUB_CLIENT_ID", "$github_client_id,"),
            ("LAUNCHPLANE_GITHUB_CLIENT_SECRET", "$github_client_secret,"),
            ("LAUNCHPLANE_PUBLIC_URL", "$public_url,"),
            ("LAUNCHPLANE_SESSION_SECRET", "$session_secret,"),
            ("GH_TOKEN", "$work_graph_gh_token"),
        )
        for key, value in service_env_payload:
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/deploy-launchplane.yml",
                        key=key,
                        value=value,
                    ),
                    "launchplane_self_bootstrap",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/other.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/deploy-launchplane.yml",
                        key=key,
                        value="$unexpected_source,",
                    ),
                    "",
                )

        thin_connectors = (
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "idempotency-key",
                "${{ steps.product.outputs.publish_idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "idempotency-key",
                "${{ steps.product.outputs.publish_inputs_idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "repository",
                "${{ steps.source.outputs.repository }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "RESOLVED_IMAGE_REPOSITORY",
                "${{ steps.publish_inputs.outputs.image_repository }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "RESOLVED_DEVKIT_REPOSITORY",
                "${{ steps.publish_inputs.outputs.devkit_repository }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "RESOLVED_SHARED_ADDONS_REPOSITORY",
                "${{ steps.publish_inputs.outputs.shared_addons_repository }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "token",
                "${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "ODOO_SOURCE_GITHUB_TOKEN",
                "${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "GITHUB_TOKEN",
                "${{ github.token }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "GHCR_USERNAME",
                "${{ github.repository_owner }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "username",
                "${{ github.repository_owner }}",
            ),
            (
                ".github/workflows/reusable-odoo-post-deploy.yml",
                "idempotency-key",
                "${{ steps.product.outputs.idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-prod-promotion.yml",
                "idempotency-key",
                "${{ steps.product.outputs.idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-prod-rollback.yml",
                "idempotency-key",
                "${{ steps.product.outputs.idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-testing-deploy.yml",
                "idempotency-key",
                "${{ steps.product.outputs.idempotency_key }}",
            ),
            (".github/workflows/ci.yml", "context", "."),
            (
                ".github/workflows/launchplane-deploy.yml",
                "IMAGE_REPOSITORY",
                "${{ steps.image_metadata.outputs.image_repository }}",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "idempotency-key",
                "generic-web-deploy:${{ vars.LAUNCHPLANE_PRODUCT }}:${{ vars.LAUNCHPLANE_CONTEXT }}:${{ vars.LAUNCHPLANE_INSTANCE }}:${{ github.event.workflow_run.head_sha }}:${{ steps.launchplane_payload.outputs.artifact_id }}",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "password",
                "${{ github.token }}",
            ),
            (
                ".github/workflows/odoo-driver-route-smoke.yml",
                "odoo-driver-route-smoke",
                "${{ env.PRODUCT }}:${{ env.CONTEXT_NAME }}",
            ),
            (
                ".github/workflows/odoo-driver-route-smoke.yml",
                "odoo-driver-route-smoke",
                "${{ env.PRODUCT }}:${{",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "checkout.repository[1]",
                "${{ github.repository_owner }}/launchplane",
                {"launchplane_tool_checkout_pinned_blocks": {"1"}},
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-product-repo-config-authority.yml@main",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-stable-deploy.yml@main",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@main",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-verification.yml@main",
            ),
            (
                ".github/workflows/reusable-generic-web-preview-verification.yml",
                "verification.timeout_seconds",
                "${{ inputs['timeout-seconds'] }}",
            ),
            (
                ".github/workflows/reusable-generic-web-preview-verification.yml",
                "verification.checked_urls",
                "${{ inputs.checked_urls }}",
            ),
            (
                ".github/workflows/reusable-generic-web-preview-verification.yml",
                "verification.verified_at",
                "${{ steps.request.outputs.verified_at }}",
            ),
        )
        for case in thin_connectors:
            path, key, value, *context = case
            allow_context = context[0] if context else None
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(
                        path=path,
                        key=key,
                        value=value,
                        allow_context=allow_context,
                    ),
                    "thin_connector_input",
                )

        rejected_thin_connectors = (
            (
                ".github/workflows/launchplane-deploy.yml",
                "uses",
                "cbusillo/not-launchplane/.github/workflows/reusable-generic-web-stable-deploy.yml@main",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-stable-deploy.yml@feature",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/not-launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@main",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@feature",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/not-launchplane/.github/workflows/reusable-generic-web-preview-verification.yml@main",
            ),
            (
                ".github/workflows/preview.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-verification.yml@feature",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "idempotency-key",
                "${{ inputs.idempotency_key }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "repository",
                "${{ vars.DEFAULT_REPOSITORY }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "repository",
                "$repository",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "RESOLVED_IMAGE_REPOSITORY",
                "${{ vars.IMAGE_REPOSITORY }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "ODOO_SOURCE_GITHUB_TOKEN",
                "${{ secrets.OTHER_TOKEN }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "token",
                "${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || 'literal-token' }}",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "GITHUB_TOKEN",
                "write",
            ),
            (
                ".github/workflows/generic-workflow.yml",
                "IMAGE_REPOSITORY",
                "${{ steps.image_metadata.outputs.image_repository }}",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "IMAGE_REPOSITORY",
                "ghcr.io/example/product",
            ),
            (
                ".github/workflows/launchplane-deploy.yml",
                "idempotency-key",
                "generic-web-deploy:${{ vars.LAUNCHPLANE_PRODUCT }}:${{ secrets.RUNTIME_SECRET }}:${{ github.event.workflow_run.head_sha }}:${{ steps.launchplane_payload.outputs.artifact_id }}",
            ),
            (
                ".github/workflows/generic-workflow.yml",
                "password",
                "${{ github.token }}",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "checkout.repository[1]",
                "cbusillo/launchplane",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "checkout.repository[1]",
                "${{ github.repository_owner }}/other-tool",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "repository",
                "${{ github.repository_owner }}/launchplane",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "checkout.repository[1]",
                "${{ github.repository_owner }}/launchplane",
            ),
            (
                ".github/workflows/launchplane-config-authority.yml",
                "uses",
                "cbusillo/launchplane/.github/workflows/reusable-product-repo-config-authority.yml@feature",
            ),
        )
        for path, key, value in rejected_thin_connectors:
            with self.subTest(path=path, key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=path,
                        key=key,
                        value=value,
                    ),
                    "",
                )

        operator_supplied_values = (
            ("launchplane_url", "${{ vars.LAUNCHPLANE_PUBLIC_URL }}"),
            ("product", "${{ vars.LAUNCHPLANE_PRODUCT }}"),
            ("instance", "${{ vars.LAUNCHPLANE_INSTANCE }}"),
        )
        for key, value in operator_supplied_values:
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/launchplane-deploy.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )

        self.assertEqual(
            _allow_reason(
                path=".github/workflows/launchplane-deploy.yml",
                key="product",
                value="${{ vars.DEFAULT_REPOSITORY }}",
            ),
            "",
        )

    def test_reusable_odoo_workflow_aliases_are_path_scoped(self) -> None:
        reusable_workflow_cases = (
            (
                ".github/workflows/reusable-odoo-post-deploy.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
                ("PRODUCT_INPUT", "${{ inputs.product }}"),
            ),
            (
                ".github/workflows/reusable-odoo-prod-promotion.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
                ("PRODUCT_INPUT", "${{ inputs.product }}"),
            ),
            (
                ".github/workflows/reusable-odoo-prod-rollback.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
                ("PRODUCT_INPUT", "${{ inputs.product }}"),
            ),
            (
                ".github/workflows/reusable-odoo-testing-deploy.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
                ("PRODUCT_INPUT", "${{ inputs.product }}"),
            ),
        )

        for path, *aliases in reusable_workflow_cases:
            for key, value in aliases:
                with self.subTest(path=path, key=key):
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value=value),
                        "operator_supplied_runtime_input",
                    )
                    self.assertEqual(
                        _allow_reason(
                            path=".github/workflows/generic-workflow.yml",
                            key=key,
                            value=value,
                        ),
                        "",
                    )
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value="hard-coded-product"),
                        "",
                    )

        for path in (
            ".github/workflows/reusable-odoo-post-deploy.yml",
            ".github/workflows/reusable-odoo-prod-promotion.yml",
            ".github/workflows/reusable-odoo-prod-rollback.yml",
            ".github/workflows/reusable-odoo-testing-deploy.yml",
        ):
            with self.subTest(path=path, key="idempotency-key"):
                self.assertEqual(
                    _allow_reason(
                        path=path,
                        key="idempotency-key",
                        value="${{ inputs.idempotency_key }}",
                    ),
                    "",
                )

    def test_product_context_workflow_aliases_are_path_scoped(self) -> None:
        workflow_aliases = (
            (
                ".github/workflows/product-context-cutover.yml",
                ("SOURCE_CONTEXT", "${{ inputs.source_context }}"),
                ("TARGET_CONTEXT", "${{ inputs.target_context }}"),
            ),
            (
                ".github/workflows/product-context-cutover-audit.yml",
                ("SOURCE_CONTEXT", "${{ inputs.source_context }}"),
                ("TARGET_CONTEXT", "${{ inputs.target_context }}"),
                ("PREVIEW_CONTEXT", "${{ inputs.preview_context }}"),
            ),
            (
                ".github/workflows/product-legacy-context-cleanup.yml",
                ("SOURCE_CONTEXT", "${{ inputs.source_context }}"),
                ("TARGET_CONTEXT", "${{ inputs.target_context }}"),
            ),
        )

        for path, *aliases in workflow_aliases:
            for key, value in aliases:
                with self.subTest(path=path, key=key):
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value=value),
                        "operator_supplied_runtime_input",
                    )
                    self.assertEqual(
                        _allow_reason(
                            path=".github/workflows/generic-workflow.yml",
                            key=key,
                            value=value,
                        ),
                        "",
                    )
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value="hard-coded-context"),
                        "",
                    )

    def test_remaining_workflow_aliases_are_path_scoped(self) -> None:
        workflow_aliases = (
            (
                ".github/workflows/merge-train-policy-import.yml",
                ("POLICY_BASE_BRANCH", "${{ inputs.base_branch }}"),
            ),
            (
                ".github/workflows/odoo-config-parameter-override.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
            ),
            (
                ".github/workflows/odoo-driver-route-smoke.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
            ),
            (
                ".github/workflows/odoo-website-bootstrap-override.yml",
                ("CONTEXT_NAME", "${{ inputs.context }}"),
            ),
            (
                ".github/workflows/product-environment-evidence.yml",
                ("TARGET_SET", "${{ inputs.target_set }}"),
                ("PROVIDER_ID", "${{ inputs.provider_id }}"),
            ),
            (
                ".github/workflows/provider-target-operations.yml",
                ("TARGET_SET", "${{ inputs.target_set }}"),
                ("PROVIDER_ID", "${{ inputs.provider_id }}"),
            ),
            (
                ".github/workflows/runner-lane-registration.yml",
                ("LANE_NAME", "${{ inputs.lane_name }}"),
            ),
        )

        for path, *aliases in workflow_aliases:
            for key, value in aliases:
                with self.subTest(path=path, key=key):
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value=value),
                        "operator_supplied_runtime_input",
                    )
                    self.assertEqual(
                        _allow_reason(
                            path=".github/workflows/generic-workflow.yml",
                            key=key,
                            value=value,
                        ),
                        "",
                    )
                    self.assertEqual(
                        _allow_reason(path=path, key=key, value="hard-coded-value"),
                        "",
                    )

    def test_artifact_publish_operator_aliases_are_path_scoped(self) -> None:
        for key, value in (
            ("INPUT_PRODUCT", "${{ inputs.product }}"),
            ("CONTEXT_NAME", "${{ inputs.context }}"),
            ("INSTANCE_NAME", "${{ inputs.instance }}"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/reusable-odoo-artifact-publish.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_workflow_jq_response_fields_are_classified_narrowly(self) -> None:
        for key, value in (
            ("environment", "$environment,"),
            ("context", "$environment_detail.context,"),
            ("target_type", "$target.target_type,"),
            ("provider_target_type", "$target.provider_target_type,"),
            ("target_id_recorded", "$target.target_id_recorded,"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/product-environment-evidence.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/other.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for key, value in (
            ("environment", "$product,"),
            ("target_type", "$target.provider_target_type,"),
            ("context", "production"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/product-environment-evidence.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for path, key, value in (
            (".github/workflows/product-context-cutover.yml", "source_context", "$source_context,"),
            (".github/workflows/product-context-cutover.yml", "target_context", "$target_context,"),
            (
                ".github/workflows/product-legacy-context-cleanup.yml",
                "source_context",
                "$source_context,",
            ),
            (
                ".github/workflows/product-legacy-context-cleanup.yml",
                "target_context",
                "$target_context,",
            ),
            (".github/workflows/provider-target-operations.yml", "provider_id", "$provider_id,"),
            (".github/workflows/provider-target-operations.yml", "context", "$context,"),
            (".github/workflows/provider-target-operations.yml", "instance", "$instance,"),
        ):
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "operator_supplied_runtime_input",
                )

    def test_preview_feedback_url_output_forward_is_narrow(self) -> None:
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/preview-control-plane.yml",
                key="preview_url",
                value="${{ needs.provision_preview.outputs.preview_url }}",
            ),
            "thin_connector_input",
        )

        for value in (
            "https://pr-186.syo-preview.example.test",
            "${{ needs.other_job.outputs.preview_url }}",
            "${{ vars.PREVIEW_URL }}",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/preview-control-plane.yml",
                        key="preview_url",
                        value=value,
                    ),
                    "",
                )

    def test_workflow_block_fields_are_classified_by_path_only(self) -> None:
        for path, key, value in (
            (".github/workflows/edge-endpoint-apply.yml", "endpoint_key", "$endpoint_key,"),
            (".github/workflows/odoo-config-parameter-override.yml", "key", "$key,"),
        ):
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "operator_supplied_runtime_input",
                )
                self.assertEqual(
                    _allow_reason(path=path, key=key, value="hard-coded"),
                    "",
                )

        for path, key, value in (
            (
                ".github/workflows/odoo-driver-route-smoke.yml",
                "ROUTE_PATHS",
                "/v1/drivers/odoo/artifact-publish-inputs "
                "/v1/drivers/odoo/preview-apply-inputs "
                "/v1/drivers/odoo/preview-apply /v1/previews/pr-feedback",
            ),
            (
                ".github/workflows/product-context-cutover-audit.yml",
                "key",
                'claims.get(key, "")',
            ),
            (
                ".github/workflows/provider-target-operations.yml",
                "path",
                "provider-target-routes.json provider-target-operation-results/*.json",
            ),
            (
                ".github/workflows/reusable-odoo-artifact-publish.yml",
                "GITHUB_TOKEN",
                "${{ github.token }}",
            ),
        ):
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )
                self.assertEqual(
                    _allow_reason(path=path, key=key, value="hard-coded"),
                    "",
                )

        self.assertEqual(
            _allow_reason(
                path=".github/workflows/provider-target-operations.yml",
                key="context",
                value=".result.context,",
            ),
            "thin_connector_input",
        )
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/other.yml",
                key="endpoint_key",
                value="$endpoint_key,",
            ),
            "",
        )

    def test_remaining_workflow_generated_idempotency_keys_are_path_scoped(
        self,
    ) -> None:
        route_smoke_key = (
            "odoo-driver-route-smoke:${{ env.PRODUCT }}:${{ env.CONTEXT_NAME }}:${{ "
            "env.INSTANCE }}:run-${{ github.run_id }}-attempt-${{ github.run_attempt }}"
        )
        for path, key, value in (
            (
                ".github/workflows/odoo-driver-route-smoke.yml",
                "idempotency-key",
                route_smoke_key,
            ),
            (
                ".github/workflows/public-ingress-monitor.yml",
                "idempotency-key",
                "public-ingress-monitor:${{ github.run_id }}:${{ github.run_attempt }}",
            ),
        ):
            with self.subTest(path=path, key=key):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )
                self.assertEqual(
                    _allow_reason(path=path, key=key, value="${{ inputs.idempotency_key }}"),
                    "",
                )

    def test_generic_web_preview_verification_workflow_defaults_are_thin_connector_inputs(
        self,
    ) -> None:
        for key, value in (
            ("inputs.timeout-ms.default", "300000"),
            ("inputs.timeout-seconds.default", "null"),
        ):
            with self.subTest(key=key):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/reusable-generic-web-preview-verification.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )

    def test_product_driver_reusable_workflow_mechanics_are_path_scoped(
        self,
    ) -> None:
        path = ".github/workflows/reusable-product-driver-prod-promotion.yml"
        path_scoped_cases = (
            ("launchplane_url", "${{ inputs.launchplane_url }}"),
            ("timeout-ms", "${{ inputs['timeout-ms'] }}"),
            ("FROM_INSTANCE", "${{ inputs.from_instance }}"),
            ("TO_INSTANCE", "${{ inputs.to_instance }}"),
            ("inputs.from_instance.default", "testing"),
            ("inputs.to_instance.default", "prod"),
        )
        for key, value in path_scoped_cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/generic-workflow.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        globally_thin_cases = (
            ("route-path", "${{ steps.request.outputs.route_path }}"),
            ("idempotency-key", "${{ steps.request.outputs.idempotency_key }}"),
            ("payload-fields.promotion.artifact_id", "${{ inputs.artifact_id }}"),
            ("payload-fields.promotion.to_instance", "${{ steps.request.outputs.to_instance }}"),
        )
        for key, value in globally_thin_cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )

        for key, value in (
            ("idempotency-key", "${{ inputs.idempotency_key }}"),
            ("payload-fields.promotion.artifact_id", "ghcr.io/example/app:latest"),
            ("payload-fields.promotion.provider_target", "${{ inputs.provider_target }}"),
            ("payload-fields.promotion.artifact_id", "${{ github.token }}"),
            ("action", "drop-prod"),
            ("action", "reset-testing"),
            ("instance", "testing"),
            ("intent", "operator-local-reset"),
            ("intent", "stable-testing-reset"),
            ("launchplane_url", "https://example.test"),
            ("TO_INSTANCE", "${{ github.token }}"),
            ("inputs.to_instance.default", "production"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(_allow_reason(path=path, key=key, value=value), "")

    def test_product_driver_operation_wrapper_literals_are_exact_path_scoped(
        self,
    ) -> None:
        allowed_cases = (
            (
                ".github/workflows/reusable-product-driver-prod-launch-readiness.yml",
                "instance",
                "prod",
            ),
            (
                ".github/workflows/reusable-product-driver-testing-reset.yml",
                "instance",
                "testing",
            ),
            (
                ".github/workflows/reusable-product-driver-testing-reset.yml",
                "action",
                "reset-testing",
            ),
            (
                ".github/workflows/reusable-product-driver-testing-reset.yml",
                "intent",
                "stable-testing-reset",
            ),
        )
        for path, key, value in allowed_cases:
            with self.subTest(path=path, key=key, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key=key, value=value),
                    "thin_connector_input",
                )

        rejected_cases = (
            (
                ".github/workflows/reusable-product-driver-prod-promotion.yml",
                "instance",
                "testing",
            ),
            (
                ".github/workflows/reusable-product-driver-prod-launch-readiness.yml",
                "instance",
                "testing",
            ),
            (
                ".github/workflows/reusable-product-driver-testing-reset.yml",
                "action",
                "drop-prod",
            ),
            (
                ".github/workflows/reusable-product-driver-testing-reset.yml",
                "intent",
                "operator-local-reset",
            ),
        )
        for path, key, value in rejected_cases:
            with self.subTest(path=path, key=key, value=value):
                self.assertEqual(_allow_reason(path=path, key=key, value=value), "")

    def test_workflow_output_references_are_thin_connector_inputs(self) -> None:
        for key, value in (
            ("PRIMARY_BASE_URL", "${{ needs.resolve.outputs.primary_base_url }}"),
            ("healthcheck_path", "${{ needs.resolve.outputs.healthcheck_path }}"),
            ("BASE_URL", "${{ needs.verify.outputs.base_url }}"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/prod-launch-readiness.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )

        for key, value in (
            ("PRIMARY_BASE_URL", "https://example.test"),
            ("instance", "${{ needs.resolve.outputs.instance }}"),
            ("instance", "prod"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/prod-launch-readiness.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_ingress_workflow_jq_forwards_and_route_options_are_narrow(self) -> None:
        for key, value in (
            ("domain_names", "[$domain],"),
            ("domain_names", "[ $domain ]"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/ingress-route-dry-run.yml",
                        key=key,
                        value=value,
                    ),
                    "operator_supplied_runtime_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/other.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for key, value in (
            ("npmplus_http3_support", "true,"),
            ("npmplus_noindex", "false"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/ingress-route-dry-run.yml",
                        key=key,
                        value=value,
                    ),
                    "thin_connector_input",
                )
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/other.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

        for key, value in (
            ("CANARY_PRODUCT", "launchplane"),
            ("CANARY_CONTEXT", "reon-prod"),
            ("domain_names", "[real.example.test]"),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(
                    _allow_reason(
                        path=".github/workflows/ingress-route-canary-apply.yml",
                        key=key,
                        value=value,
                    ),
                    "",
                )

    def test_dokploy_target_setup_launchplane_product_is_self_management(self) -> None:
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/dokploy-target-setup.yml",
                key="product",
                value="launchplane",
            ),
            "launchplane_self_bootstrap",
        )
        self.assertEqual(
            _allow_reason(
                path=".github/workflows/dokploy-target-setup.yml",
                key="product",
                value='"launchplane",',
            ),
            "launchplane_self_bootstrap",
        )

        for path, value in (
            (".github/workflows/other.yml", "launchplane"),
            (".github/workflows/other.yml", '"launchplane",'),
            (".github/workflows/dokploy-target-setup.yml", "other-product"),
        ):
            with self.subTest(path=path, value=value):
                self.assertEqual(
                    _allow_reason(path=path, key="product", value=value),
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
            fail_on_findings_result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--fail-on-findings",
                ],
            )

        self.assertEqual(json_result.exit_code, 0, json_result.output)
        self.assertEqual(markdown_result.exit_code, 0, markdown_result.output)
        self.assertEqual(changed_files_result.exit_code, 0, changed_files_result.output)
        self.assertNotEqual(fail_on_findings_result.exit_code, 0)
        self.assertIn(
            "Config authority audit found unallowed checked-in runtime authority.",
            fail_on_findings_result.output,
        )
        self.assertEqual(json.loads(json_result.output)["mode"], "full-audit")
        self.assertEqual(
            json.loads(changed_files_result.output)["mode"],
            "changed-files-gate",
        )
        self.assertIn("# Config Authority Audit", markdown_result.output)

    def test_cli_fail_on_findings_allows_documented_examples(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            docs = root / "docs" / "product-repo-contract.md"
            docs.parent.mkdir()
            docs.write_text(
                "Example rejected product repo fixture: PRODUCT_DOMAIN=example.test\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--fail-on-findings",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["findings"])
        self.assertTrue(
            all(finding["classification"] == "allowed" for finding in payload["findings"])
        )

    def test_product_repo_gate_rejects_launchplane_lifecycle_test_fixtures(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            fixture = root / "tests" / "fixtures" / "launchplane-runtime.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text(
                json.dumps(
                    {
                        "runtimeEnvironment": {
                            "product": "real-product",
                            "providerTargetId": "dokploy-target-123",
                        }
                    }
                ),
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)
            default_gate = evaluate_config_authority_gate(payload)
            product_repo_gate = evaluate_config_authority_gate(payload, profile="product-repo")

        self.assertEqual(default_gate["status"], "pass")
        self.assertEqual(product_repo_gate["status"], "fail")
        rejected = cast("list[dict[str, object]]", product_repo_gate["rejected_findings"])
        self.assertTrue(rejected)
        self.assertTrue(
            all(
                finding["rejection_reason"] == "launchplane_lifecycle_test_fixture"
                for finding in rejected
            )
        )

    def test_product_repo_gate_allows_product_owned_test_fixtures(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            fixture = root / "tests" / "fixtures" / "checkout-flow.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text(
                json.dumps(
                    {
                        "cartTotal": 42,
                        "shippingDomain": "checkout.example.test",
                        "browserEnvironment": "mobile",
                    }
                ),
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)
            product_repo_gate = evaluate_config_authority_gate(payload, profile="product-repo")

        self.assertEqual(_findings(payload), [])
        self.assertEqual(product_repo_gate["status"], "pass")

    def test_product_repo_gate_allows_product_owned_service_password_fixtures(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "tests.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Tests\n"
                "jobs:\n"
                "  mysql:\n"
                "    runs-on: ubuntu-latest\n"
                "    services:\n"
                "      mysql:\n"
                "        image: mysql:8.0\n"
                "        env:\n"
                "          MYSQL_ROOT_PASSWORD: root\n"
                "    steps:\n"
                "      - run: echo ok\n"
                "        env:\n"
                "          MYSQL_PASSWORD: root\n",
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)
            product_repo_gate = evaluate_config_authority_gate(payload, profile="product-repo")

        self.assertTrue(
            any(finding["rule_id"] == "secret_binding_identity" for finding in _findings(payload))
        )
        self.assertEqual(product_repo_gate["status"], "pass")

    def test_product_repo_gate_rejects_managed_secret_binding_fixtures(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            fixture = root / "tests" / "fixtures" / "config.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text(
                json.dumps({"managedSecretBinding": "DOKPLOY_TOKEN"}),
                encoding="utf-8",
            )
            _commit_all(root)

            payload = build_config_authority_audit(control_plane_root=root)
            product_repo_gate = evaluate_config_authority_gate(payload, profile="product-repo")

        self.assertEqual(product_repo_gate["status"], "fail")
        rejected = cast("list[dict[str, object]]", product_repo_gate["rejected_findings"])
        self.assertTrue(
            any(
                finding["rejection_reason"] == "launchplane_lifecycle_test_fixture"
                for finding in rejected
            )
        )

    def test_cli_changed_files_gate_fails_on_new_product_repo_fixture(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Deploy\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/product-fixture")
            workflow.write_text(
                "name: Deploy\nenv:\n  PRODUCT_DOMAIN: https://real-product.example.test\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("PRODUCT_DOMAIN", result.output)
        self.assertIn("needs_classification", result.output)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_changed_files_gate_allows_preexisting_package_json_finding(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            package_json = root / "package.json"
            package_json.write_text(
                json.dumps(
                    {
                        "scripts": {"seo:submit-indexnow": "node scripts/seo/submit-indexnow.mjs"},
                        "devDependencies": {"globals": "^17.6.0"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "dependabot/dev-dependencies")
            package_json.write_text(
                json.dumps(
                    {
                        "scripts": {"seo:submit-indexnow": "node scripts/seo/submit-indexnow.mjs"},
                        "devDependencies": {"globals": "^17.7.0"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        finding = _findings(payload)[0]
        self.assertEqual(finding["key"], "scripts.seo:submit-indexnow")
        self.assertEqual(finding["classification"], "allowed")
        self.assertEqual(finding["allow_reason"], "preexisting_changed_file_finding")

    def test_cli_changed_files_gate_allows_preexisting_dirty_package_json_finding(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            package_json = root / "package.json"
            package_json.write_text(
                json.dumps(
                    {
                        "scripts": {"seo:submit-indexnow": "node scripts/seo/submit-indexnow.mjs"},
                        "devDependencies": {"globals": "^17.6.0"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            package_json.write_text(
                json.dumps(
                    {
                        "scripts": {"seo:submit-indexnow": "node scripts/seo/submit-indexnow.mjs"},
                        "devDependencies": {"globals": "^17.7.0"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        finding = _findings(payload)[0]
        self.assertEqual(finding["allow_reason"], "preexisting_changed_file_finding")

    def test_cli_changed_files_gate_rejects_new_package_json_finding(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            package_json = root / "package.json"
            package_json.write_text(
                json.dumps({"devDependencies": {"globals": "^17.6.0"}}, indent=2) + "\n",
                encoding="utf-8",
            )
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/new-script")
            package_json.write_text(
                json.dumps(
                    {
                        "scripts": {"seo:submit-indexnow": "node scripts/seo/submit-indexnow.mjs"},
                        "devDependencies": {"globals": "^17.6.0"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")
        rejected = cast("list[dict[str, object]]", gate["rejected_findings"])
        self.assertEqual(rejected[0]["key"], "scripts.seo:submit-indexnow")

    def test_cli_changed_files_gate_rejects_new_duplicate_finding(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            script = root / "scripts" / "deploy.sh"
            script.parent.mkdir()
            script.write_text(
                "#!/usr/bin/env bash\necho cbusillo/launchplane\n",
                encoding="utf-8",
            )
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/duplicate-finding")
            script.write_text(
                "#!/usr/bin/env bash\necho cbusillo/launchplane\necho cbusillo/launchplane\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")
        findings = _findings(payload)
        self.assertEqual(findings[0]["allow_reason"], "preexisting_changed_file_finding")
        self.assertEqual(findings[1]["classification"], "needs_classification")

    def test_cli_changed_files_gate_fails_closed_without_merge_base(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            package_json = root / "package.json"
            package_json.write_text(
                json.dumps({"devDependencies": {"globals": "^17.6.0"}}, indent=2) + "\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")
        rejected = cast("list[dict[str, object]]", gate["rejected_findings"])
        self.assertEqual(rejected[0]["rule_id"], "changed_files_gate_base_unavailable")

    def test_cli_product_repo_gate_allows_launchplane_tool_checkout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "---\n"
                "name: Launchplane Config Authority\n\n"
                '"on":\n'
                "  pull_request:\n"
                "  workflow_dispatch:\n\n"
                "permissions:\n"
                "  contents: read\n\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n\n"
                "    steps:\n"
                "      - name: Check out repository\n"
                "        uses: actions/checkout@v6\n"
                "        with:\n"
                "          path: product-repo\n"
                "          fetch-depth: 0\n\n"
                "      - name: Check out Launchplane\n"
                "        uses: actions/checkout@v6\n"
                "        id: launchplane\n"
                "        with:\n"
                "          repository: ${{ github.repository_owner }}/launchplane\n"
                "          ref: 65d5695e48db61d3e491762e957a9f8101c3bf81\n"
                "          path: launchplane\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")

    def test_cli_product_repo_gate_allows_reusable_launchplane_gate_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "---\n"
                "name: Launchplane Config Authority\n\n"
                '"on":\n'
                "  pull_request:\n"
                "  workflow_dispatch:\n\n"
                "permissions:\n"
                "  contents: read\n\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-product-repo-config-authority.yml@main\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        findings = _findings(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["key"], "uses")
        self.assertEqual(findings[0]["allow_reason"], "thin_connector_input")

    def test_cli_product_repo_gate_rejects_reusable_launchplane_gate_branch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "---\n"
                "name: Launchplane Config Authority\n\n"
                '"on":\n'
                "  pull_request:\n"
                "  workflow_dispatch:\n\n"
                "permissions:\n"
                "  contents: read\n\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-product-repo-config-authority.yml@feature\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")
        findings = _findings(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["key"], "uses")
        self.assertEqual(findings[0]["allow_reason"], "")

    def test_cli_product_repo_gate_allows_image_artifact_deploy_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Deploy\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/image-deploy")
            workflow.write_text(
                "---\n"
                "name: Launchplane Deploy\n\n"
                '"on":\n'
                "  workflow_run:\n"
                "    workflows: [Test Suite]\n"
                "    types: [completed]\n\n"
                "permissions:\n"
                "  contents: read\n"
                "  id-token: write\n"
                "  packages: write\n\n"
                "jobs:\n"
                "  deploy:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "        with:\n"
                "          ref: ${{ github.event.workflow_run.head_sha }}\n"
                "      - name: Prepare image metadata\n"
                "        id: image_metadata\n"
                "        run: |\n"
                '          image_repository="ghcr.io/${GITHUB_REPOSITORY,,}"\n'
                '          image_tag="sha-${GITHUB_SHA}"\n'
                '          echo "image_repository=${image_repository}" >> "$GITHUB_OUTPUT"\n'
                '          echo "image_reference=${image_repository}:${image_tag}" >> "$GITHUB_OUTPUT"\n'
                "      - uses: docker/login-action@v4\n"
                "        with:\n"
                "          registry: ghcr.io\n"
                "          username: ${{ github.actor }}\n"
                "          password: ${{ github.token }}\n"
                "      - id: build_image\n"
                "        uses: docker/build-push-action@v7\n"
                "        with:\n"
                "          context: .\n"
                "          file: docker/Dockerfile.sync\n"
                "          push: true\n"
                "          tags: ${{ steps.image_metadata.outputs.image_reference }}\n"
                "      - id: launchplane_payload\n"
                "        env:\n"
                "          IMAGE_REPOSITORY: ${{ steps.image_metadata.outputs.image_repository }}\n"
                "          IMAGE_DIGEST: ${{ steps.build_image.outputs.digest }}\n"
                "          TESTED_SHA: ${{ github.event.workflow_run.head_sha }}\n"
                "          LAUNCHPLANE_PUBLIC_URL: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "          LAUNCHPLANE_PRODUCT: ${{ vars.LAUNCHPLANE_PRODUCT }}\n"
                "          LAUNCHPLANE_CONTEXT: ${{ vars.LAUNCHPLANE_CONTEXT }}\n"
                "          LAUNCHPLANE_INSTANCE: ${{ vars.LAUNCHPLANE_INSTANCE }}\n"
                "        run: |\n"
                '          artifact_id="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"\n'
                '          echo "artifact_id=${artifact_id}" >> "$GITHUB_OUTPUT"\n'
                "      - uses: cbusillo/launchplane/.github/actions/launchplane-request@main\n"
                "        with:\n"
                "          launchplane-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "          route-path: /v1/drivers/generic-web/deploy\n"
                "          payload-file: ${{ steps.launchplane_payload.outputs.payload_path }}\n"
                "          idempotency-key: >-\n"
                "            generic-web-deploy:${{ vars.LAUNCHPLANE_PRODUCT }}:${{ vars.LAUNCHPLANE_CONTEXT }}:${{ vars.LAUNCHPLANE_INSTANCE }}:${{ github.event.workflow_run.head_sha }}:${{ steps.launchplane_payload.outputs.artifact_id }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        thin_connector_keys = {
            finding["key"]
            for finding in _findings(payload)
            if finding["allow_reason"] == "thin_connector_input"
        }
        self.assertTrue(
            {"password", "context", "file", "IMAGE_REPOSITORY", "idempotency-key"}
            <= thin_connector_keys
        )

    def test_cli_product_repo_gate_allows_reusable_generic_web_deploy_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-deploy.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Deploy\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/reusable-image-deploy")
            workflow.write_text(
                "---\n"
                "name: Launchplane Deploy\n\n"
                '"on":\n'
                "  workflow_run:\n"
                "    workflows: [Test Suite]\n"
                "    types: [completed]\n\n"
                "permissions:\n"
                "  contents: read\n"
                "  id-token: write\n"
                "  packages: write\n\n"
                "jobs:\n"
                "  build-image:\n"
                "    runs-on: ubuntu-latest\n"
                "    outputs:\n"
                "      artifact_id: ${{ steps.launchplane_artifact.outputs.artifact_id }}\n"
                "      source_git_ref: ${{ github.event.workflow_run.head_sha }}\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "        with:\n"
                "          ref: ${{ github.event.workflow_run.head_sha }}\n"
                "      - uses: docker/login-action@v4\n"
                "        with:\n"
                "          registry: ghcr.io\n"
                "          username: ${{ github.actor }}\n"
                "          password: ${{ github.token }}\n"
                "      - id: build_image\n"
                "        uses: docker/build-push-action@v7\n"
                "        with:\n"
                "          context: .\n"
                "          file: docker/Dockerfile.sync\n"
                "          push: true\n"
                "          tags: ${{ steps.image_metadata.outputs.image_reference }}\n"
                "      - id: launchplane_artifact\n"
                "        run: |\n"
                '          artifact_id="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"\n'
                '          echo "artifact_id=${artifact_id}" >> "$GITHUB_OUTPUT"\n'
                "  deploy:\n"
                "    needs: build-image\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-stable-deploy.yml@main\n"
                "    with:\n"
                "      launchplane_url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "      artifact_id: ${{ needs.build-image.outputs.artifact_id }}\n"
                "      source_git_ref: ${{ needs.build-image.outputs.source_git_ref }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        thin_connector_keys = {
            finding["key"]
            for finding in _findings(payload)
            if finding["allow_reason"] == "thin_connector_input"
        }
        self.assertIn("uses", thin_connector_keys)
        self.assertNotIn("product", thin_connector_keys)
        self.assertNotIn("instance", thin_connector_keys)

    def test_cli_product_repo_gate_allows_product_driver_stable_deploy_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "publish-image.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Publish Image\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/product-driver-stable-deploy")
            workflow.write_text(
                "---\n"
                "name: Publish Image\n\n"
                '"on":\n'
                "  push:\n"
                "    branches: [main]\n\n"
                "jobs:\n"
                "  publish:\n"
                "    runs-on: ubuntu-latest\n"
                "    outputs:\n"
                "      artifact_id: ${{ steps.image.outputs.artifact_id }}\n"
                "      source_git_ref: ${{ github.sha }}\n"
                "    steps:\n"
                "      - id: image\n"
                "        run: echo 'artifact_id=ghcr.io/example/product:sha' >> \"$GITHUB_OUTPUT\"\n"
                "  deploy:\n"
                "    needs: publish\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-product-driver-stable-deploy.yml@main\n"
                "    with:\n"
                "      launchplane_url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "      artifact_id: ${{ needs.publish.outputs.artifact_id }}\n"
                "      source_git_ref: ${{ needs.publish.outputs.source_git_ref }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        thin_connector_keys = {
            finding["key"]
            for finding in _findings(payload)
            if finding["allow_reason"] == "thin_connector_input"
        }
        self.assertIn("uses", thin_connector_keys)
        self.assertNotIn("product", thin_connector_keys)
        self.assertNotIn("context", thin_connector_keys)
        self.assertNotIn("instance", thin_connector_keys)

    def test_cli_product_repo_gate_allows_reusable_generic_web_preview_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "preview.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Preview\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/reusable-preview")
            workflow.write_text(
                "---\n"
                "name: Preview\n\n"
                '"on":\n'
                "  pull_request:\n"
                "    types: [opened, synchronize, reopened, labeled, unlabeled, closed]\n\n"
                "permissions:\n"
                "  contents: read\n"
                "  id-token: write\n"
                "  packages: write\n\n"
                "jobs:\n"
                "  build-image:\n"
                "    runs-on: ubuntu-latest\n"
                "    outputs:\n"
                "      image_reference: ${{ steps.image.outputs.image_reference }}\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "      - id: image\n"
                "        run: echo 'image_reference=ghcr.io/example/app@sha256:test' >> \"$GITHUB_OUTPUT\"\n"
                "  preview:\n"
                "    needs: build-image\n"
                "    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@main\n"
                "    with:\n"
                "      operation: refresh\n"
                "      launchplane_url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}\n"
                "      anchor_pr_number: ${{ github.event.pull_request.number }}\n"
                "      anchor_pr_url: ${{ github.event.pull_request.html_url }}\n"
                "      anchor_head_sha: ${{ github.event.pull_request.head.sha }}\n"
                "      image_reference: ${{ needs.build-image.outputs.image_reference }}\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")
        thin_connector_keys = {
            finding["key"]
            for finding in _findings(payload)
            if finding["allow_reason"] == "thin_connector_input"
        }
        self.assertIn("uses", thin_connector_keys)
        self.assertNotIn("preview_slug", thin_connector_keys)
        self.assertNotIn("preview_url", thin_connector_keys)
        self.assertNotIn("idempotency-key", thin_connector_keys)

    def test_cli_product_repo_gate_allows_compact_launchplane_tool_checkout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "        with:\n"
                "          repository: ${{ github.repository_owner }}/launchplane\n"
                "          ref: 65d5695e48db61d3e491762e957a9f8101c3bf81\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "pass")

    def test_cli_product_repo_gate_rejects_hardcoded_launchplane_tool_checkout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "        with:\n"
                "          repository: cbusillo/launchplane\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("repository", result.output)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_product_repo_gate_rejects_mutable_launchplane_tool_checkout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v6\n"
                "        with:\n"
                "          repository: ${{ github.repository_owner }}/launchplane\n"
                "          ref: main\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_product_repo_gate_rejects_mutable_launchplane_with_other_pinned_checkout(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - name: Other pinned checkout\n"
                "        uses: actions/checkout@v6\n"
                "        with:\n"
                "          repository: ${{ github.repository }}\n"
                "          ref: 65d5695e48db61d3e491762e957a9f8101c3bf81\n"
                "      - name: Mutable Launchplane checkout\n"
                "        uses: actions/checkout@v6\n"
                "        with:\n"
                "          repository: ${{ github.repository_owner }}/launchplane\n"
                "          ref: main\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_product_repo_gate_rejects_non_checkout_launchplane_repository(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "env:\n"
                "  repository: ${{ github.repository_owner }}/launchplane\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_product_repo_gate_rejects_nested_launchplane_repository_with_block(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            workflow = root / ".github" / "workflows" / "launchplane-config-authority.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Launchplane Config Authority\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/config-authority-gate")
            workflow.write_text(
                "name: Launchplane Config Authority\n"
                "jobs:\n"
                "  launchplane-config-authority:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - name: Check out Launchplane\n"
                "        uses: actions/checkout@v6\n"
                "        env:\n"
                "          with:\n"
                "            repository: ${{ github.repository_owner }}/launchplane\n"
                "            ref: 65d5695e48db61d3e491762e957a9f8101c3bf81\n",
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")

    def test_cli_product_repo_gate_fails_on_changed_lifecycle_fixture(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _init_repo(root)
            fixture = root / "tests" / "fixtures" / "launchplane-runtime.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("{}\n", encoding="utf-8")
            _commit_all(root)
            _git(root, "branch", "-M", "main")
            _checkout_branch(root, "feature/product-fixture")
            fixture.write_text(
                '{"runtimeEnvironment":{"providerTargetId":"dokploy-target-123"}}\n',
                encoding="utf-8",
            )
            _commit_all(root)

            runner = CliRunner()
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "audit-config-authority",
                    "--control-plane-root",
                    str(root),
                    "--mode",
                    "changed-files-gate",
                    "--fail-on-findings",
                    "--gate-profile",
                    "product-repo",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("providerTargetId", result.output)
        self.assertIn("test_fixture", result.output)
        payload = json.loads(result.output.split("Error:", 1)[0])
        gate = cast("dict[str, object]", payload["gate"])
        self.assertEqual(gate["status"], "fail")
        rejected = cast("list[dict[str, object]]", gate["rejected_findings"])
        self.assertTrue(
            any(
                finding["rejection_reason"] == "launchplane_lifecycle_test_fixture"
                for finding in rejected
            )
        )

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
