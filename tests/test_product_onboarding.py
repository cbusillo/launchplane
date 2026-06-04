import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from click import Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest


CLI_MAIN = cast(Command, main)
ODOO_RUNTIME_KEYS = (
    "ODOO_DB_NAME",
    "ODOO_DB_USER",
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
)
ODOO_SECRET_KEYS = (
    "ODOO_ADMIN_PASSWORD",
    "ODOO_DB_PASSWORD",
    "ODOO_MASTER_PASSWORD",
)
SYO_RUNTIME_KEYS = (
    "CONTACT_EMAIL_MODE",
    "CONTACT_FROM_EMAIL",
    "CONTACT_TO_EMAIL",
    "CONTACT_EMAIL_RESEND_TIMEOUT_MS",
    "NEXT_PUBLIC_META_PIXEL_ID",
)


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _manifest_payload() -> dict[str, object]:
    return {
        "product": "example-site",
        "display_name": "Example Site",
        "repository": "cbusillo/example-site",
        "driver_id": "generic-web",
        "image_repository": "ghcr.io/cbusillo/example-site",
        "runtime_port": 3000,
        "health_path": "/api/health",
        "lanes": [
            {
                "instance": "testing",
                "context": "example-site-testing",
                "base_url": "https://testing.example.invalid",
                "public_ingress_monitoring": {
                    "alert_issue_url": "https://github.com/cbusillo/launchplane/issues/929"
                },
                "odoo_stable_bootstrap": {
                    "enabled": True,
                    "approval_issue_url": "https://github.com/cbusillo/launchplane/issues/573",
                    "confirmation": "bootstrap example testing",
                    "expected_target_name": "example-site-testing",
                    "expected_domains": ["testing.example.invalid"],
                },
                "odoo_data_policy": {
                    "data_authority": "resettable",
                    "allowed_rebuild_sources": ["empty"],
                    "requires_backup_before_destroy": False,
                    "requires_restore_proof": False,
                    "requires_runtime_identity": True,
                },
            },
            {
                "instance": "prod",
                "context": "example-site-prod",
                "base_url": "https://example.invalid",
                "health_url": "https://example.invalid/status",
                "odoo_prelaunch_rebuild": {
                    "enabled": True,
                    "approval_issue_url": "https://github.com/cbusillo/launchplane/issues/573",
                    "data_source_mode": "upstream_restore",
                    "confirmation": "restore example upstream",
                    "expected_target_name": "example-site-prod",
                    "expected_domains": ["example.invalid"],
                },
                "odoo_data_policy": {
                    "data_authority": "restorable",
                    "allowed_rebuild_sources": ["upstream_restore"],
                    "upstream_source": "example-site/prod/upstream",
                    "requires_backup_before_destroy": True,
                    "requires_restore_proof": True,
                    "requires_runtime_identity": True,
                },
            },
        ],
        "preview": {
            "enabled": True,
            "context": "example-site-preview",
            "enable_label": "preview-requested",
            "slug_template": "pr-{number}",
        },
        "provider_targets": [
            {
                "context": "example-site-testing",
                "instance": "testing",
                "target_id": "app-testing-123",
                "target_type": "application",
                "target_name": "example-site-testing",
                "domains": ["testing.example.invalid"],
            },
            {
                "context": "example-site-prod",
                "instance": "prod",
                "target_id": "app-prod-123",
                "target_type": "application",
                "target_name": "example-site-prod",
                "domains": ["example.invalid"],
                "require_prod_gate": True,
            },
        ],
        "runtime_environments": [
            {
                "scope": "instance",
                "context": "example-site-testing",
                "instance": "testing",
                "env": {"PUBLIC_BASE_URL": "https://testing.example.invalid"},
            }
        ],
        "secret_bindings": [
            {
                "binding_key": "SMTP_PASSWORD",
                "context": "example-site-prod",
                "instance": "prod",
            }
        ],
        "expected_config": {
            "runtime_environment_keys": [
                {
                    "key": "PUBLIC_BASE_URL",
                    "context": "example-site-testing",
                    "instance": "testing",
                }
            ],
            "managed_secret_bindings": [
                {
                    "binding_key": "SMTP_PASSWORD",
                    "context": "example-site-prod",
                    "instance": "prod",
                }
            ],
        },
        "updated_at": "2026-05-03T01:30:00Z",
        "source_label": "test:onboarding",
    }


def _seed_import_manifest(
    import_id: str, target_ids_by_env: dict[str, str] | None = None
) -> dict[str, object]:
    catalog = json.loads(
        Path("import-material/launchplane/seed-imports/catalog.json").read_text(encoding="utf-8")
    )
    entry = next(item for item in catalog["imports"] if item["import_id"] == import_id)
    manifest_payload = json.loads(json.dumps(entry["manifest"]))
    target_ids = target_ids_by_env or {}
    for mapping in entry.get("target_id_env", []):
        targets = manifest_payload.get("provider_targets", [])
        for target in targets:
            if (
                target["context"] == mapping["context"]
                and target["instance"] == mapping["instance"]
            ):
                target["target_id"] = target_ids.get(mapping["env"], "")
    return cast(dict[str, object], manifest_payload)


def _assert_odoo_stable_lane_runtime_contract(
    test_case: unittest.TestCase,
    *,
    manifest: ProductOnboardingManifest,
    context: str,
    expected_database_names: dict[str, str],
) -> None:
    runtime_records = {
        record.instance: record
        for record in manifest.runtime_environments
        if record.context == context
    }
    test_case.assertEqual(set(runtime_records), set(expected_database_names))
    for instance, expected_database_name in expected_database_names.items():
        runtime_record = runtime_records[instance]
        test_case.assertEqual(runtime_record.scope, "instance")
        volume_prefix = f"{context}_{instance}"
        test_case.assertEqual(
            runtime_record.env,
            {
                "ODOO_DB_NAME": expected_database_name,
                "ODOO_DB_USER": "odoo",
                "ODOO_DATA_VOLUME": f"{volume_prefix}_odoo_data",
                "ODOO_LOG_VOLUME": f"{volume_prefix}_odoo_logs",
                "ODOO_DB_VOLUME": f"{volume_prefix}_odoo_db",
            },
        )

    secret_bindings = [
        (binding.context, binding.instance, binding.binding_key)
        for binding in manifest.secret_bindings
        if binding.context == context
    ]
    test_case.assertEqual(
        secret_bindings,
        [
            (context, instance, binding_key)
            for instance in expected_database_names
            for binding_key in ODOO_SECRET_KEYS
        ],
    )
    test_case.assertEqual(
        [
            (requirement.context, requirement.instance, requirement.key)
            for requirement in manifest.expected_config.runtime_environment_keys
        ],
        [
            (context, instance, key)
            for instance in expected_database_names
            for key in ODOO_RUNTIME_KEYS
        ],
    )
    test_case.assertEqual(
        [
            (requirement.context, requirement.instance, requirement.binding_key)
            for requirement in manifest.expected_config.managed_secret_bindings
        ],
        [
            (context, instance, binding_key)
            for instance in expected_database_names
            for binding_key in ODOO_SECRET_KEYS
        ],
    )


class ProductOnboardingTests(unittest.TestCase):
    def test_launchplane_seed_import_workflow_owns_seed_writes(self) -> None:
        deploy_script = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")
        workflow_text = Path(".github/workflows/launchplane-seed-import.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("/v1/product-onboarding/apply", deploy_script)
        self.assertNotIn("/v1/runtime-key-safety/policies/apply", deploy_script)
        self.assertNotIn("import-material/launchplane/seed-imports/catalog.json", deploy_script)
        self.assertIn("launchplane-seed-import.yml", deploy_script)
        self.assertIn("deploy:launchplane-seed-import-product-onboarding-grant", deploy_script)
        self.assertIn("deploy:launchplane-seed-import-runtime-key-safety-grant", deploy_script)
        onboarding_label_index = deploy_script.index(
            "deploy:launchplane-seed-import-product-onboarding-grant"
        )
        onboarding_grant_block = deploy_script[
            max(0, onboarding_label_index - 240) : onboarding_label_index + 120
        ]
        self.assertIn("product_onboarding.apply", onboarding_grant_block)
        self.assertNotIn("launchplane_service_deploy.execute", onboarding_grant_block)
        self.assertIn("import-material/launchplane/seed-imports/catalog.json", workflow_text)
        self.assertIn("APPLY LAUNCHPLANE SEED IMPORTS", workflow_text)
        self.assertIn("--apply", workflow_text)
        self.assertIn("launchplane-seed-import", workflow_text)

    def test_launchplane_seed_import_catalog_validates_contracts(self) -> None:
        catalog = json.loads(
            Path("import-material/launchplane/seed-imports/catalog.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(catalog["schema_version"], 1)
        import_ids = {entry["import_id"] for entry in catalog["imports"]}
        self.assertEqual(
            import_ids,
            {
                "discord-blue-product-onboarding",
                "sellyouroutboard-product-onboarding",
                "verireel-product-onboarding",
                "odoo-cm-product-onboarding",
                "odoo-opw-product-onboarding",
                "launchplane-runtime-key-safety-policy",
            },
        )
        for entry in catalog["imports"]:
            if entry["kind"] == "product_onboarding":
                manifest_payload = json.loads(json.dumps(entry["manifest"]))
                for mapping in entry.get("target_id_env", []):
                    targets = manifest_payload.get("provider_targets", [])
                    for target in targets:
                        if (
                            target["context"] == mapping["context"]
                            and target["instance"] == mapping["instance"]
                        ):
                            target["target_id"] = f"test-{mapping['env'].lower()}"
                ProductOnboardingManifest.model_validate(manifest_payload)
            elif entry["kind"] == "runtime_key_safety_policy":
                self.assertEqual(
                    [rule["binding_key"] for rule in entry["rules"]],
                    [
                        "DISCORD_TOKEN",
                        "RESEND_API_KEY",
                        "SMTP_PASSWORD",
                        "META_CONVERSIONS_API_TOKEN",
                        "BETTER_AUTH_SECRET",
                        "VERIREEL_SECRETS_MASTER_KEY",
                        "VERIREEL_CRON_SECRET",
                        "POSTGRES_PASSWORD",
                        "ODOO_ADMIN_PASSWORD",
                        "ODOO_DB_PASSWORD",
                        "ODOO_MASTER_PASSWORD",
                    ],
                )
                odoo_rules = {
                    rule["binding_key"]: rule
                    for rule in entry["rules"]
                    if rule["binding_key"].startswith("ODOO_")
                }
                for binding_key in ODOO_SECRET_KEYS:
                    self.assertEqual(odoo_rules[binding_key]["secret_class"], "shared_safe")
                    self.assertEqual(odoo_rules[binding_key]["allowed_contexts"], ["cm", "opw"])
                    self.assertEqual(
                        odoo_rules[binding_key]["allowed_instances"], ["testing", "prod"]
                    )
            else:
                self.fail(f"Unexpected seed import kind: {entry['kind']}")

    def test_launchplane_seed_import_script_requires_target_id_env(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            output_dir = Path(temporary_directory_name) / "seed-import"
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/deploy/apply-launchplane-seed-imports.py",
                    "--catalog",
                    "import-material/launchplane/seed-imports/catalog.json",
                    "--output-dir",
                    str(output_dir),
                    "--import-id",
                    "odoo-cm-product-onboarding",
                    "--reason",
                    "test dry run",
                ],
                check=False,
                capture_output=True,
                env={"PATH": os.environ["PATH"]},
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ODOO_CM_TESTING_DOKPLOY_TARGET_ID is required", result.stderr)

    def test_launchplane_seed_import_script_patches_provider_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            output_dir = temporary_directory / "seed-import"
            catalog_path = temporary_directory / "catalog.json"
            manifest_payload = _manifest_payload()
            for target in cast(list[dict[str, object]], manifest_payload["provider_targets"]):
                if target["instance"] == "prod":
                    target["target_id"] = ""
            catalog_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "imports": [
                            {
                                "import_id": "example-provider-targets",
                                "kind": "product_onboarding",
                                "manifest": manifest_payload,
                                "target_id_env": [
                                    {
                                        "context": "example-site-prod",
                                        "instance": "prod",
                                        "env": "EXAMPLE_PROD_TARGET_ID",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/deploy/apply-launchplane-seed-imports.py",
                    "--catalog",
                    str(catalog_path),
                    "--output-dir",
                    str(output_dir),
                    "--import-id",
                    "example-provider-targets",
                    "--reason",
                    "test dry run",
                ],
                check=False,
                capture_output=True,
                env={**os.environ, "EXAMPLE_PROD_TARGET_ID": "app-prod-patched"},
                text=True,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            output_payload = json.loads(
                (output_dir / "example-provider-targets-payload.json").read_text(encoding="utf-8")
            )

        manifest = output_payload["manifest"]
        self.assertIn("provider_targets", manifest)
        self.assertNotIn("dokploy_targets", manifest)
        provider_targets = manifest["provider_targets"]
        self.assertEqual(provider_targets[1]["target_id"], "app-prod-patched")

    def test_deploy_authz_grants_include_scheduled_merge_train_runner(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("deploy:merge-train-runner-manual-grant", script_text)
        self.assertIn("merge-train-runner-manual", script_text)
        self.assertIn("deploy:merge-train-runner-schedule-grant", script_text)
        self.assertIn("merge-train-runner-schedule", script_text)
        self.assertIn("schedule", script_text)

    def test_deploy_authz_grants_include_reusable_odoo_stable_workflows(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        expected_workflows = (
            "reusable-odoo-artifact-publish.yml",
            "reusable-odoo-testing-deploy.yml",
            "reusable-odoo-post-deploy.yml",
            "reusable-odoo-prod-promotion.yml",
            "reusable-odoo-prod-rollback.yml",
        )
        for workflow_file in expected_workflows:
            self.assertIn(workflow_file, script_text)
        for context_name in ("cm", "opw"):
            self.assertIn(
                f"deploy:odoo-{context_name}-artifact-publish-inputs-grant",
                script_text,
            )
            self.assertIn(f"deploy:odoo-{context_name}-artifact-publish-grant", script_text)
            self.assertIn(
                f"deploy:odoo-{context_name}-testing-target-replacement-grant",
                script_text,
            )
            self.assertIn(f"deploy:odoo-{context_name}-post-deploy-grant", script_text)
            self.assertIn(f"deploy:odoo-{context_name}-prod-promotion-run-grant", script_text)
            self.assertIn(f"deploy:odoo-{context_name}-prod-rollback-grant", script_text)
        self.assertIn("deploy:odoo-cm-website-bootstrap-override-grant", script_text)
        self.assertIn("deploy:odoo-opw-website-bootstrap-override-grant", script_text)
        self.assertIn("odoo-cm-website-bootstrap-override", script_text)
        self.assertIn("odoo-opw-website-bootstrap-override", script_text)
        self.assertNotIn('base_url: "https://opw-testing.shinycomputers.com"', script_text)
        self.assertNotIn('health_path: "/launchplane/health"', script_text)
        self.assertNotIn('domains: ["opw-testing.shinycomputers.com"]', script_text)

    def test_reusable_odoo_artifact_publish_standardizes_request_shape(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-artifact-publish.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_call", workflow_text)
        self.assertIn("product:", workflow_text)
        self.assertIn('product="odoo-tenant-${CONTEXT_NAME}"', workflow_text)
        self.assertIn("/v1/drivers/odoo/artifact-publish-inputs", workflow_text)
        self.assertIn("/v1/drivers/odoo/artifact-publish", workflow_text)
        self.assertIn("product=${{ steps.product.outputs.product }}", workflow_text)
        self.assertIn("odoo-artifact-publish-inputs", workflow_text)
        self.assertIn("odoo-artifact-publish", workflow_text)
        self.assertIn(
            "${{ steps.product.outputs.publish_inputs_idempotency_key }}",
            workflow_text,
        )
        self.assertIn("${{ steps.product.outputs.publish_idempotency_key }}", workflow_text)
        self.assertIn("fail-result-paths: result.input_status", workflow_text)
        self.assertIn("fail-result-paths: result.status,result.publish_status", workflow_text)
        self.assertIn("token: ${{ secrets.ODOO_SOURCE_GITHUB_TOKEN }}", workflow_text)
        self.assertIn("inputs.source_git_ref=${{ github.sha }}", workflow_text)
        self.assertIn(
            "RESOLVED_IMAGE_REPOSITORY: >-\n"
            "            ${{ steps.publish_inputs.outputs.image_repository }}",
            workflow_text,
        )
        self.assertIn(
            "RESOLVED_IMAGE_TAG: ${{ steps.publish_inputs.outputs.image_tag }}",
            workflow_text,
        )
        self.assertIn("publish.manifest=${{ steps.publish.outputs.manifest_file }}", workflow_text)
        self.assertNotIn("short_sha=", workflow_text)
        self.assertNotIn("IMAGE_REPOSITORY: ghcr.io/${{ github.repository }}", workflow_text)

    def test_reusable_odoo_testing_deploy_uses_tenant_product_scope(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-testing-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("/v1/drivers/odoo/target-replacement-apply", workflow_text)
        self.assertIn('product="odoo-tenant-${CONTEXT_NAME}"', workflow_text)
        self.assertIn("product=${{ steps.product.outputs.product }}", workflow_text)
        self.assertIn('"instance":"testing"', workflow_text)
        self.assertIn("replacement.artifact_id=${{ inputs.artifact_id }}", workflow_text)
        self.assertIn(
            "odoo_target_replacement_apply",
            Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8"),
        )

    def test_odoo_website_bootstrap_override_workflow_allows_opw_targets(self) -> None:
        workflow_text = Path(".github/workflows/odoo-website-bootstrap-override.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("odoo-tenant-opw:opw:testing", workflow_text)
        self.assertIn("odoo-tenant-opw:opw:prod", workflow_text)
        self.assertIn("          - opw", workflow_text)
        self.assertIn("          - prod", workflow_text)
        self.assertNotIn("writes only cm/testing", workflow_text)

    def test_launchplane_workflows_do_not_hardcode_public_service_defaults(self) -> None:
        workflow_dir = Path(".github/workflows")
        forbidden_literals = (
            "https://launchplane.shinycomputers.com",
            "launchplane.shinycomputers.com",
        )

        for workflow_path in workflow_dir.glob("*.yml"):
            workflow_text = workflow_path.read_text(encoding="utf-8")
            for literal in forbidden_literals:
                with self.subTest(workflow=workflow_path.name, literal=literal):
                    self.assertNotIn(literal, workflow_text)

    def test_reusable_odoo_workflows_accept_configured_service_identity(self) -> None:
        workflow_paths = (
            Path(".github/workflows/reusable-odoo-artifact-publish.yml"),
            Path(".github/workflows/reusable-odoo-testing-deploy.yml"),
            Path(".github/workflows/reusable-odoo-post-deploy.yml"),
            Path(".github/workflows/reusable-odoo-prod-promotion.yml"),
            Path(".github/workflows/reusable-odoo-prod-rollback.yml"),
        )

        for workflow_path in workflow_paths:
            workflow_text = workflow_path.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow_path.name):
                self.assertIn("launchplane_url:", workflow_text)
                self.assertIn("launchplane_audience:", workflow_text)
                self.assertIn(
                    "inputs.launchplane_url || vars.LAUNCHPLANE_PUBLIC_URL", workflow_text
                )
                self.assertIn("audience: ${{ inputs.launchplane_audience }}", workflow_text)

    def test_ingress_route_dry_run_workflow_rejects_non_object_options(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-dry-run.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('($options | type) != "object"', workflow_text)
        self.assertIn('error("route_options_json must be a JSON object")', workflow_text)
        self.assertIn(
            'error("route_options_json contains unsupported route option key(s)")',
            workflow_text,
        )
        inputs_section = workflow_text.split("permissions:", maxsplit=1)[0]
        self.assertEqual(inputs_section.count("        description:"), 10)
        self.assertNotIn("identity_access_provider:", inputs_section)
        self.assertNotIn("identity_access_send_basic_auth:", inputs_section)
        self.assertIn("categories=\\($categories)", workflow_text)
        self.assertIn(
            "npmplus_noindex: false",
            workflow_text,
        )
        self.assertIn("def option($key; $default):", workflow_text)
        self.assertIn("if $options | has($key)", workflow_text)
        self.assertIn("} + $route_options", workflow_text)
        self.assertIn("require_exact_expected_host_domains: option", workflow_text)
        self.assertIn("allow_create: option", workflow_text)
        self.assertIn("allow_update: option", workflow_text)
        self.assertIn("allow_enable_disable: option", workflow_text)
        for route_option in (
            "require_exact_expected_host_domains",
            "allow_create",
            "allow_update",
            "allow_enable_disable",
            "hsts_enabled",
            "hsts_subdomains",
            "trust_forwarded_proto",
            "npmplus_crowdsec_appsec",
            "npmplus_proxy_request_buffering",
            "npmplus_proxy_response_buffering",
            "npmplus_upstream_compression",
            "npmplus_fancyindex",
            "npmplus_x_frame_options",
            "npmplus_auth_request",
            "identity_access",
            "advanced_config",
            "locations",
        ):
            self.assertIn(f'"{route_option}"', workflow_text)
        for forward_scheme in ("http", "https", "path", "empty", "grpc", "grpcs"):
            self.assertIn(f"          - {forward_scheme}", workflow_text)

    def test_ingress_route_audit_read_workflow_is_plan_scoped_get(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-audit-read.yml").read_text(
            encoding="utf-8"
        )
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("curl -sS", workflow_text)
        self.assertIn("-w '%{http_code}'", workflow_text)
        self.assertIn('"$read_url"', workflow_text)
        self.assertIn("/v1/ingress/route-audits/records", workflow_text)
        self.assertIn("product", workflow_text)
        self.assertIn("context", workflow_text)
        self.assertIn("record_id", workflow_text)
        self.assertIn("limit must be between 1 and 100", workflow_text)
        self.assertIn(
            'raw_response="$RUNNER_TEMP/ingress-route-audit-read-raw.json"', workflow_text
        )
        self.assertIn("redacted", workflow_text)
        self.assertIn("operation_count", workflow_text)
        self.assertIn("ingress-route-audit-read.yml", script_text)
        self.assertIn("deploy:ingress-route-audit-read-plan-grant", script_text)
        self.assertIn("ingress_route.plan", script_text)
        self.assertNotIn("launchplane-request", workflow_text)
        self.assertNotIn("ingress_route.apply", workflow_text)
        self.assertNotIn("provider_host_id:", workflow_text)
        self.assertNotIn("idempotency-key:", workflow_text)

    def test_ingress_route_canary_apply_workflow_requires_apply_guards(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-canary-apply.yml").read_text(
            encoding="utf-8"
        )
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn('mode: "apply"', workflow_text)
        self.assertIn("idempotency-key: ${{ inputs.idempotency_key }}", workflow_text)
        self.assertIn("CONFIRMATION: ${{ inputs.confirmation }}", workflow_text)
        self.assertIn("apply ingress canary", workflow_text)
        self.assertIn("CANARY_DOMAIN: ${{ vars.LAUNCHPLANE_INGRESS_CANARY_DOMAIN }}", workflow_text)
        self.assertIn(
            "CANARY_EXPECTED_HOST_ID: ${{ vars.LAUNCHPLANE_INGRESS_CANARY_HOST_ID }}", workflow_text
        )
        self.assertIn("require_exact_expected_host_domains: true", workflow_text)
        inputs_section = workflow_text.split("permissions:", maxsplit=1)[0]
        self.assertNotIn("      domain:", inputs_section)
        self.assertNotIn("      expected_host_id:", inputs_section)
        self.assertIn('forward_scheme: "http"', workflow_text)
        self.assertIn("npmplus_noindex: true", workflow_text)
        self.assertIn("categories=\\($categories)", workflow_text)
        self.assertNotIn("route_options_json", workflow_text)
        self.assertIn("ingress-route-canary-apply.yml", script_text)
        self.assertIn("deploy:ingress-route-canary-apply-grant", script_text)
        self.assertIn("ingress_route.apply", script_text)

    def test_reusable_odoo_testing_deploy_exposes_result_outputs(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-testing-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("replacement.source_git_ref=${{ inputs.source_git_ref }}", workflow_text)
        self.assertIn("outputs:", workflow_text)
        self.assertIn(
            "value: ${{ jobs.testing-deploy.outputs.deployment_record_id }}", workflow_text
        )
        self.assertIn(
            "deployment_record_id: ${{ steps.poll.outputs.deployment_record_id }}", workflow_text
        )
        for result_path in (
            "deploy_status",
            "post_deploy_status",
            "health_status",
            "canonical_status",
            "logo_status",
        ):
            self.assertIn(result_path, workflow_text)

    def test_deploy_launchplane_keeps_terminal_agent_and_owner_agent_inputs_separate(
        self,
    ) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("omit_terminal_agent_env", workflow_text)
        self.assertIn("omit_owner_agent_env", workflow_text)
        self.assertIn(
            "omit_terminal_agent_env:\n"
            "        description: Temporarily omit terminal-agent env for one compatibility deploy.\n"
            "        required: false\n"
            "        default: false",
            workflow_text,
        )
        self.assertIn(
            "omit_owner_agent_env:\n"
            "        description: Temporarily omit owner-agent env for one compatibility deploy.\n"
            "        required: false\n"
            "        default: false",
            workflow_text,
        )
        self.assertIn("if ($omit_terminal_agent_env | not) then", workflow_text)
        self.assertIn("if ($omit_owner_agent_env | not) then", workflow_text)
        self.assertIn("LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", workflow_text)

    def test_deploy_launchplane_omit_npmplus_env_removes_existing_keys(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("service_env_removals_json=", workflow_text)
        self.assertIn("oauth_env_removals: $service_env_removals", workflow_text)
        for env_key in (
            "LAUNCHPLANE_NPMPLUS_BASE_URL",
            "LAUNCHPLANE_NPMPLUS_IDENTITY",
            "LAUNCHPLANE_NPMPLUS_SECRET",
        ):
            self.assertIn(f'"{env_key}"', workflow_text)
        self.assertIn("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", workflow_text)
        self.assertIn("LAUNCHPLANE_LOCAL_ADMIN_TOKEN", workflow_text)
        self.assertNotIn(
            "--argjson omit_terminal_agent_env '${{ inputs.omit_terminal_agent_env != false }}'",
            workflow_text,
        )

    def test_deploy_launchplane_break_glass_rollback_uploads_evidence(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK=true", workflow_text)
        self.assertIn("break_glass_image_reference is required", workflow_text)
        self.assertIn("break_glass_reason is required", workflow_text)
        self.assertIn("scripts/deploy/emergency-dokploy-rollback.py", workflow_text)
        self.assertIn("LAUNCHPLANE_ROLLBACK_REASON", workflow_text)
        self.assertNotIn(
            "from control_plane import dokploy as control_plane_dokploy", workflow_text
        )
        self.assertIn("inputs.break_glass_confirm == ''", workflow_text)
        self.assertIn(
            "inputs.break_glass_confirm == 'ROLL BACK LAUNCHPLANE THROUGH DIRECT DOKPLOY'",
            workflow_text,
        )
        self.assertIn("launchplane-break-glass-rollback.json", workflow_text)
        self.assertIn("name: launchplane-break-glass-rollback", workflow_text)
        self.assertIn("uses: actions/upload-artifact@v7", workflow_text)
        self.assertIn("Evidence artifact: launchplane-break-glass-rollback", workflow_text)
        self.assertIn("manual break-glass only", workflow_text)

    def test_deploy_authz_grants_seed_local_admin_self_deploy_authority(self) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("local-admin \\", script_text)
        self.assertIn("deploy:local-admin-self-deploy-grant", script_text)
        self.assertIn("local-admin-self-deploy", script_text)
        self.assertIn("launchplane_service_deploy.execute", script_text)

    def test_deploy_authz_grants_stage_dedicated_policy_grant_authority(self) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("deploy-launchplane.yml", script_text)
        self.assertIn("authz_policy_grant.write", script_text)
        self.assertIn("deploy:authz-policy-grant-maintenance-dispatch", script_text)
        self.assertIn("authz-policy-grant-maintenance-dispatch", script_text)
        self.assertIn("deploy:authz-policy-grant-maintenance-run", script_text)
        self.assertIn("authz-policy-grant-maintenance-run", script_text)
        self.assertIn("workflow_run", script_text)

    def test_deploy_authz_grants_do_not_restore_stale_import_self_deploy_rules(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertNotIn("/v1/authz-policies/github-actions/removals", script_text)
        self.assertNotIn("stale-launchplane-seed-import-self-deploy", script_text)
        self.assertNotIn("stale-merge-train-policy-import-self-deploy", script_text)
        self.assertIn("product_onboarding.apply", script_text)
        self.assertIn("runtime_key_safety.write", script_text)
        self.assertIn("merge_train.policy_import", script_text)

    def test_deploy_authz_grants_include_phase_two_live_target_runtime_scopes(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q 'live-target-runtime.yml'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_grants = temporary_directory / "grants.jsonl"
            captured_grants.touch()
            captured_response_file = temporary_directory / "response.json"
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "DISCORD_BLUE_DOKPLOY_TARGET_ID": "app-discord-blue",
                "ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing",
                "ODOO_CM_PROD_DOKPLOY_TARGET_ID": "compose-cm-prod",
                "ODOO_OPW_TESTING_DOKPLOY_TARGET_ID": "compose-opw-testing",
                "ODOO_OPW_PROD_DOKPLOY_TARGET_ID": "compose-opw-prod",
                "CAPTURED_GRANTS": str(captured_grants),
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            grants = [
                json.loads(grant_text)["grant"]
                for grant_text in captured_grants.read_text().split("---END-GRANT---")
                if grant_text.strip()
            ]

        grant_index = {
            (grant["products"][0], grant["contexts"][0], grant["actions"][0]): grant
            for grant in grants
        }
        expected_scopes = {
            ("sellyouroutboard", "sellyouroutboard"),
            ("discord-blue", "discord-blue"),
            ("verireel", "verireel"),
            ("odoo-tenant-cm", "cm"),
            ("odoo-tenant-opw", "opw"),
        }
        for product, context in expected_scopes:
            for action in ("live_target_runtime.plan", "live_target_runtime.apply"):
                grant = grant_index[(product, context, action)]
                self.assertEqual(grant["repository"], "cbusillo/launchplane")
                self.assertEqual(
                    grant["workflow_refs"],
                    [
                        "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                    ],
                )
                self.assertEqual(grant["event_names"], ["workflow_dispatch"])

    def test_reusable_odoo_prod_promotion_fails_on_each_result_status(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-prod-promotion.yml").read_text(
            encoding="utf-8"
        )

        for result_path in (
            "result.run_status",
            "result.promotion_status",
            "result.deployment_status",
            "result.post_deploy_status",
            "result.destination_health_status",
        ):
            self.assertIn(result_path, workflow_text)

        self.assertIn("fail-result-paths", workflow_text)

    def test_deploy_authz_grants_include_opw_manual_preview_workflow(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q 'odoo-tenant-opw/.github/workflows/odoo-preview.yml'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_grants = temporary_directory / "grants.jsonl"
            captured_grants.touch()
            captured_response_file = temporary_directory / "response.json"
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "DISCORD_BLUE_DOKPLOY_TARGET_ID": "app-discord-blue",
                "ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing",
                "ODOO_CM_PROD_DOKPLOY_TARGET_ID": "compose-cm-prod",
                "ODOO_OPW_TESTING_DOKPLOY_TARGET_ID": "compose-opw-testing",
                "ODOO_OPW_PROD_DOKPLOY_TARGET_ID": "compose-opw-prod",
                "CAPTURED_GRANTS": str(captured_grants),
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            grants = [
                json.loads(grant_text)["grant"]
                for grant_text in captured_grants.read_text().split("---END-GRANT---")
                if grant_text.strip()
            ]

        grant_index = {
            (grant["products"][0], grant["actions"][0], grant["source_label"]): grant
            for grant in grants
        }
        expected_grants = {
            (
                "odoo-tenant-opw",
                "odoo_artifact_publish_inputs.read",
                "deploy:odoo-opw-preview-artifact-publish-inputs-manual-grant",
            ): ["workflow_dispatch"],
            (
                "odoo",
                "odoo_artifact_publish.write",
                "deploy:odoo-opw-preview-artifact-publish-grant",
            ): ["workflow_dispatch"],
            (
                "odoo-tenant-opw",
                "preview_refresh.execute",
                "deploy:odoo-opw-preview-refresh-grant",
            ): ["pull_request"],
            (
                "odoo-tenant-opw",
                "preview_pr_feedback.write",
                "deploy:odoo-opw-preview-pr-feedback-grant",
            ): ["pull_request"],
            (
                "odoo-tenant-opw",
                "preview_pr_feedback.write",
                "deploy:odoo-opw-preview-unsupported-feedback-grant",
            ): ["pull_request_target"],
            (
                "odoo-tenant-opw",
                "preview_destroy.execute",
                "deploy:odoo-opw-preview-destroy-pr-grant",
            ): ["pull_request"],
            (
                "odoo-tenant-opw",
                "preview_destroy.execute",
                "deploy:odoo-opw-preview-destroy-manual-grant",
            ): ["workflow_dispatch"],
            (
                "odoo-tenant-opw",
                "odoo_preview_apply.execute",
                "deploy:odoo-opw-preview-apply-grant",
            ): ["pull_request"],
            (
                "odoo-tenant-opw",
                "odoo_preview_apply.execute",
                "deploy:odoo-opw-preview-apply-manual-grant",
            ): ["workflow_dispatch"],
        }

        self.assertLessEqual(set(expected_grants), set(grant_index))
        for key, event_names in expected_grants.items():
            grant = grant_index[key]
            self.assertEqual(grant["repository"], "cbusillo/odoo-tenant-opw")
            self.assertEqual(grant["contexts"], ["opw"])
            self.assertEqual(grant["event_names"], event_names)
            self.assertEqual(
                grant["workflow_refs"],
                ["cbusillo/odoo-tenant-opw/.github/workflows/odoo-preview.yml@*"],
            )

    def test_deploy_authz_grants_scope_public_ingress_monitor_to_launchplane(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q 'public-ingress-monitor.yml'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_grants = temporary_directory / "grants.jsonl"
            captured_grants.touch()
            captured_response_file = temporary_directory / "response.json"
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "DISCORD_BLUE_DOKPLOY_TARGET_ID": "app-discord-blue",
                "ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing",
                "ODOO_CM_PROD_DOKPLOY_TARGET_ID": "compose-cm-prod",
                "ODOO_OPW_TESTING_DOKPLOY_TARGET_ID": "compose-opw-testing",
                "ODOO_OPW_PROD_DOKPLOY_TARGET_ID": "compose-opw-prod",
                "CAPTURED_GRANTS": str(captured_grants),
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            grants = [
                json.loads(grant_text)["grant"]
                for grant_text in captured_grants.read_text().split("---END-GRANT---")
                if grant_text.strip()
            ]

        self.assertEqual(len(grants), 2)
        grant_index = {grant["event_names"][0]: grant for grant in grants}
        self.assertEqual(set(grant_index), {"schedule", "workflow_dispatch"})
        for grant in grants:
            self.assertEqual(grant["repository"], "cbusillo/launchplane")
            self.assertEqual(
                grant["workflow_refs"],
                [
                    "cbusillo/launchplane/.github/workflows/public-ingress-monitor.yml@refs/heads/main"
                ],
            )
            self.assertEqual(grant["products"], ["launchplane"])
            self.assertEqual(grant["contexts"], ["launchplane"])
            self.assertEqual(grant["actions"], ["public_ingress_monitor.run_once"])

    def test_deploy_authz_grants_include_terminal_agent_product_profile_read(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("product_profile.read", script_text)
        self.assertIn("deploy:terminal-agent-product-profile-read-grant", script_text)
        self.assertIn("terminal-agent-product-profile-read", script_text)

    def test_deploy_authz_grants_include_local_operator_notification_apply(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("/v1/authz-policies/local-operators/grants", script_text)
        self.assertIn("public_ingress_notification_policy.apply", script_text)
        self.assertIn(
            "deploy:local-operator-public-ingress-notification-policy-grant",
            script_text,
        )

    def test_deploy_authz_grants_skip_local_operator_product_config_without_scopes(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q '\"grant\"'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_grants = temporary_directory / "grants.jsonl"
            captured_grants.touch()
            captured_response_file = temporary_directory / "response.json"
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "CAPTURED_GRANTS": str(captured_grants),
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            grants = [
                json.loads(grant_text)["grant"]
                for grant_text in captured_grants.read_text().split("---END-GRANT---")
                if grant_text.strip()
            ]

        product_config_grants = [
            grant
            for grant in grants
            if grant["actions"][0].startswith("product_config.")
            and "subjects" in grant
            and "token_labels" in grant
        ]
        self.assertEqual(product_config_grants, [])
        self.assertIn(
            "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON is unset or empty; skipping local-operator product_config.plan grant reconciliation.",
            result.stdout,
        )
        self.assertIn(
            "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON is unset or empty; skipping local-operator product_config.apply grant reconciliation.",
            result.stdout,
        )

    def test_deploy_authz_grants_fail_on_malformed_local_operator_product_config_scopes(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_response_file = temporary_directory / "response.json"
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON": "not-json",
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("parse error", result.stderr)

    def test_deploy_authz_grants_scope_configured_local_operator_product_config(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q '\"grant\"'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                '    if [ -n "$output_file" ]; then\n'
                '      printf \'{"status":"ok"}\' > "$output_file"\n'
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_grants = temporary_directory / "grants.jsonl"
            captured_grants.touch()
            captured_response_file = temporary_directory / "response.json"
            configured_scopes = json.dumps(
                [
                    {"product": "discord-blue", "context": "discord-blue"},
                    {"product": "verireel", "context": "verireel"},
                ]
            )
            env = {
                **os.environ,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON": configured_scopes,
                "CAPTURED_GRANTS": str(captured_grants),
                "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                "CAPTURED_BIN_DIR": str(captured_bin_directory),
            }

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            grants = [
                json.loads(grant_text)["grant"]
                for grant_text in captured_grants.read_text().split("---END-GRANT---")
                if grant_text.strip()
            ]

        product_config_grants = [
            grant
            for grant in grants
            if grant["actions"][0].startswith("product_config.")
            and "subjects" in grant
            and "token_labels" in grant
        ]
        scoped_grants = {
            (grant["products"][0], grant["contexts"][0], grant["actions"][0])
            for grant in product_config_grants
        }
        expected_scopes = {
            ("discord-blue", "discord-blue"),
            ("verireel", "verireel"),
        }

        self.assertEqual(
            scoped_grants,
            {
                (product, context, action)
                for product, context in expected_scopes
                for action in ("product_config.plan", "product_config.apply")
            },
        )
        for grant in product_config_grants:
            self.assertNotEqual(grant["products"], ["*"])
            self.assertNotEqual(grant["contexts"], ["*"])
            self.assertTrue(
                grant["source_label"].startswith("deploy:local-operator-product-config-")
            )

    def test_seed_import_verireel_onboarding_manifest_enrolls_preview_lifecycle(self) -> None:
        manifest_payload = _seed_import_manifest("verireel-product-onboarding")
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)

        self.assertEqual(manifest.product, "verireel")
        self.assertEqual(manifest.display_name, "VeriReel")
        self.assertEqual(manifest.repository, "cbusillo/verireel")
        self.assertEqual(manifest.driver_id, "verireel")
        self.assertEqual(manifest.image_repository, "ghcr.io/cbusillo/verireel-app")
        self.assertEqual(manifest.runtime_port, 3000)
        self.assertEqual(manifest.health_path, "/api/health")
        self.assertEqual(
            [(lane.instance, lane.context, lane.base_url) for lane in manifest.lanes],
            [
                ("testing", "verireel", "https://ver-testing.shinycomputers.com"),
                ("prod", "verireel", "https://ver-prod.shinycomputers.com"),
            ],
        )
        self.assertTrue(manifest.preview.enabled)
        self.assertEqual(manifest.preview.context, "verireel-testing")
        self.assertEqual(manifest.preview.enable_label, "preview")
        self.assertEqual(manifest.preview.slug_template, "pr-{number}")
        self.assertEqual(manifest.preview.app_name_prefix, "ver-preview")
        self.assertEqual(manifest.preview.data_transport_mode, "driver")
        self.assertEqual(
            manifest.preview.preview_url_env_keys,
            ("VERIREEL_APP_URL", "BETTER_AUTH_URL"),
        )
        self.assertEqual(
            manifest.preview.preview_domain_env_keys,
            ("LAUNCHPLANE_PREVIEW_BASE_URL",),
        )
        self.assertEqual(len(manifest.provider_targets), 0)
        self.assertEqual(len(manifest.secret_bindings), 0)
        self.assertEqual(len(manifest.runtime_environments), 1)
        preview_runtime = manifest.runtime_environments[0]
        self.assertEqual(preview_runtime.scope, "context")
        self.assertEqual(preview_runtime.context, "verireel-testing")
        self.assertEqual(
            preview_runtime.env["LAUNCHPLANE_PREVIEW_BASE_URL"],
            "https://ver-preview.shinycomputers.com",
        )
        self.assertEqual(
            [
                (requirement.context, requirement.instance, requirement.binding_key)
                for requirement in manifest.expected_config.managed_secret_bindings
            ],
            [
                ("verireel", "testing", "BETTER_AUTH_SECRET"),
                ("verireel", "testing", "VERIREEL_SECRETS_MASTER_KEY"),
                ("verireel", "testing", "VERIREEL_CRON_SECRET"),
                ("verireel", "testing", "POSTGRES_PASSWORD"),
                ("verireel", "prod", "BETTER_AUTH_SECRET"),
                ("verireel", "prod", "VERIREEL_SECRETS_MASTER_KEY"),
                ("verireel", "prod", "VERIREEL_CRON_SECRET"),
                ("verireel", "prod", "POSTGRES_PASSWORD"),
            ],
        )
        self.assertEqual(manifest.source_label, "import-material:verireel-product-onboarding")

    def test_seed_import_sellyouroutboard_onboarding_manifest_preserves_cutover_contract(
        self,
    ) -> None:
        manifest_payload = _seed_import_manifest("sellyouroutboard-product-onboarding")
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)

        self.assertEqual(manifest.product, "sellyouroutboard")
        self.assertEqual(manifest.display_name, "SellYourOutboard.com")
        self.assertEqual(manifest.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(manifest.driver_id, "generic-web")
        self.assertEqual(manifest.image_repository, "ghcr.io/cbusillo/sellyouroutboard")
        self.assertEqual(manifest.runtime_port, 3000)
        self.assertEqual(manifest.health_path, "/api/health")
        self.assertEqual(
            [(lane.instance, lane.context, lane.base_url) for lane in manifest.lanes],
            [
                ("testing", "sellyouroutboard", "https://syo-testing.shinycomputers.com"),
                ("prod", "sellyouroutboard", "https://www.sellyouroutboard.com"),
            ],
        )
        self.assertEqual(manifest.historical_contexts, ("sellyouroutboard-testing",))
        self.assertTrue(manifest.preview.enabled)
        self.assertEqual(manifest.preview.context, "sellyouroutboard")
        self.assertEqual(manifest.preview.app_name_prefix, "syo-preview")
        self.assertEqual(manifest.preview.template_instance, "testing")
        self.assertEqual(len(manifest.provider_targets), 0)
        self.assertEqual(len(manifest.runtime_environments), 0)
        self.assertEqual(len(manifest.secret_bindings), 0)
        self.assertEqual(
            [
                (requirement.context, requirement.instance, requirement.key)
                for requirement in manifest.expected_config.runtime_environment_keys
            ],
            [("sellyouroutboard", "testing", key) for key in SYO_RUNTIME_KEYS]
            + [("sellyouroutboard", "prod", key) for key in SYO_RUNTIME_KEYS],
        )
        self.assertEqual(
            [
                (requirement.context, requirement.instance, requirement.binding_key)
                for requirement in manifest.expected_config.managed_secret_bindings
            ],
            [
                ("sellyouroutboard", "testing", "RESEND_API_KEY"),
                ("sellyouroutboard", "testing", "META_CONVERSIONS_API_TOKEN"),
                ("sellyouroutboard", "prod", "RESEND_API_KEY"),
                ("sellyouroutboard", "prod", "META_CONVERSIONS_API_TOKEN"),
            ],
        )
        self.assertEqual(
            manifest.source_label,
            "import-material:sellyouroutboard-product-onboarding",
        )

    def test_seed_import_odoo_cm_onboarding_manifest_encodes_issue_backed_bootstrap_policy(
        self,
    ) -> None:
        manifest_payload = _seed_import_manifest(
            "odoo-cm-product-onboarding",
            {
                "ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing",
                "ODOO_CM_PROD_DOKPLOY_TARGET_ID": "compose-cm-prod",
            },
        )
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)

        self.assertEqual(manifest.product, "odoo-tenant-cm")
        self.assertEqual([lane.instance for lane in manifest.lanes], ["testing", "prod"])
        policies = {lane.instance: lane.odoo_stable_bootstrap for lane in manifest.lanes}
        self.assertEqual(
            policies["testing"].approval_issue_url,
            "https://github.com/cbusillo/launchplane/issues/573",
        )
        self.assertEqual(policies["testing"].confirmation, "bootstrap cm testing")
        self.assertEqual(policies["testing"].expected_target_name, "cm-testing")
        self.assertEqual(policies["testing"].expected_domains, ("cm-testing.shinycomputers.com",))
        self.assertEqual(
            policies["prod"].approval_issue_url,
            "https://github.com/cbusillo/launchplane/issues/573",
        )
        self.assertEqual(policies["prod"].confirmation, "bootstrap cm prod")
        self.assertEqual(policies["prod"].expected_target_name, "cm-prod")
        self.assertEqual(policies["prod"].expected_domains, ("cellmechanic.com",))
        self.assertEqual(
            [tuple(target.domains) for target in manifest.provider_targets],
            [
                ("cm-testing.shinycomputers.com",),
                ("cm-prod.shinycomputers.com",),
            ],
        )
        self.assertEqual(
            [
                (target.context, target.instance, target.target_type, target.target_id)
                for target in manifest.provider_targets
            ],
            [
                ("cm", "testing", "compose", "compose-cm-testing"),
                ("cm", "prod", "compose", "compose-cm-prod"),
            ],
        )
        _assert_odoo_stable_lane_runtime_contract(
            self,
            manifest=manifest,
            context="cm",
            expected_database_names={"testing": "cm_testing", "prod": "cm"},
        )
        self.assertEqual(manifest.source_label, "import-material:odoo-cm-product-onboarding")

    def test_seed_import_odoo_cm_onboarding_manifest_requires_prod_target_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "target requires target_id"):
            ProductOnboardingManifest.model_validate(
                _seed_import_manifest(
                    "odoo-cm-product-onboarding",
                    {"ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing"},
                )
            )

    def test_seed_import_odoo_opw_onboarding_manifest_encodes_upstream_restore_policy(
        self,
    ) -> None:
        manifest_payload = _seed_import_manifest(
            "odoo-opw-product-onboarding",
            {
                "ODOO_OPW_TESTING_DOKPLOY_TARGET_ID": "compose-opw-testing",
                "ODOO_OPW_PROD_DOKPLOY_TARGET_ID": "compose-opw-prod",
            },
        )
        manifest = ProductOnboardingManifest.model_validate(manifest_payload)

        self.assertEqual(manifest.product, "odoo-tenant-opw")
        self.assertTrue(manifest.preview.enabled)
        self.assertEqual(manifest.preview.context, "opw")
        self.assertEqual(manifest.preview.app_name_prefix, "odoo-tenant-opw")
        self.assertEqual(manifest.preview.template_instance, "testing")
        self.assertEqual(manifest.preview.override_env["ODOO_INSTALL_MODULES"], "opw_custom")
        self.assertEqual(manifest.preview.preview_url_env_keys, ("WEB_BASE_URL",))
        self.assertEqual(manifest.preview.data_transport_mode, "driver")
        policies = {lane.instance: lane.odoo_prelaunch_rebuild for lane in manifest.lanes}
        for instance in ("testing", "prod"):
            self.assertTrue(policies[instance].enabled)
            self.assertEqual(policies[instance].data_source_mode, "upstream_restore")
            self.assertEqual(policies[instance].confirmation, "restore opw upstream")
            self.assertEqual(
                policies[instance].approval_issue_url,
                "https://github.com/cbusillo/launchplane/issues/573",
            )
        self.assertEqual(policies["testing"].expected_target_name, "opw-testing")
        self.assertEqual(policies["prod"].expected_target_name, "opw-prod")
        self.assertEqual(
            policies["prod"].expected_domains,
            ("opw-prod.shinycomputers.com",),
        )
        _assert_odoo_stable_lane_runtime_contract(
            self,
            manifest=manifest,
            context="opw",
            expected_database_names={"testing": "opw_testing", "prod": "opw_prod"},
        )

    def test_apply_product_onboarding_manifest_writes_canonical_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())

            first_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
            )
            second_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T02:30:00Z",
            )

            profile = store.read_product_profile_record("example-site")
            targets = store.list_dokploy_target_records()
            target_ids = store.list_dokploy_target_id_records()
            provider_targets = store.list_physical_provider_target_records()
            runtime_records = store.list_runtime_environment_records()
            secret_bindings = store.list_secret_bindings()
            store.close()

        self.assertEqual(first_result.product, "example-site")
        self.assertEqual(second_result.product_profile.updated_at, "2026-05-03T02:30:00Z")
        self.assertEqual(profile.driver_id, "generic-web")
        self.assertEqual(profile.historical_contexts, ())
        self.assertEqual(profile.lanes[0].health_url, "https://testing.example.invalid/api/health")
        self.assertTrue(profile.lanes[0].odoo_stable_bootstrap.enabled)
        self.assertTrue(profile.lanes[0].public_ingress_monitoring.enabled)
        self.assertFalse(profile.lanes[0].public_ingress_monitoring.require_runtime_identity)
        self.assertEqual(
            profile.lanes[0].public_ingress_monitoring.alert_issue_url,
            "https://github.com/cbusillo/launchplane/issues/929",
        )
        self.assertEqual(
            profile.lanes[0].odoo_stable_bootstrap.approval_issue_url,
            "https://github.com/cbusillo/launchplane/issues/573",
        )
        self.assertEqual(
            profile.lanes[0].odoo_stable_bootstrap.confirmation,
            "bootstrap example testing",
        )
        self.assertEqual(profile.lanes[1].health_url, "https://example.invalid/status")
        self.assertTrue(profile.lanes[1].odoo_prelaunch_rebuild.enabled)
        self.assertEqual(
            profile.lanes[1].odoo_prelaunch_rebuild.data_source_mode,
            "upstream_restore",
        )
        self.assertEqual(profile.lanes[0].odoo_data_policy.data_authority, "resettable")
        self.assertEqual(
            profile.lanes[0].odoo_data_policy.allowed_rebuild_sources,
            ("empty",),
        )
        self.assertEqual(profile.lanes[1].odoo_data_policy.data_authority, "restorable")
        self.assertEqual(
            profile.lanes[1].odoo_data_policy.upstream_source, "example-site/prod/upstream"
        )
        self.assertEqual(profile.preview.enable_label, "preview-requested")
        self.assertEqual(profile.expected_config.runtime_environment_keys[0].key, "PUBLIC_BASE_URL")
        self.assertEqual(
            profile.expected_config.managed_secret_bindings[0].binding_key,
            "SMTP_PASSWORD",
        )
        self.assertEqual(len(targets), 2)
        self.assertEqual(len(target_ids), 2)
        self.assertEqual(len(provider_targets), 2)
        self.assertEqual(
            [(record.context, record.instance, record.target_id) for record in provider_targets],
            [
                ("example-site-prod", "prod", "app-prod-123"),
                ("example-site-testing", "testing", "app-testing-123"),
            ],
        )
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(len(secret_bindings), 1)
        self.assertEqual(secret_bindings[0].binding_key, "SMTP_PASSWORD")
        self.assertEqual(secret_bindings[0].status, "disabled")
        self.assertEqual(secret_bindings[0].created_at, first_result.secret_bindings[0].created_at)
        self.assertEqual(secret_bindings[0].updated_at, second_result.secret_bindings[0].updated_at)

    def test_apply_product_onboarding_manifest_preserves_historical_contexts(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest_payload = _manifest_payload()
            manifest_payload["historical_contexts"] = [
                "example-site-old",
                "example-site-preview-old",
            ]
            manifest = ProductOnboardingManifest.model_validate(manifest_payload)

            result = apply_product_onboarding_manifest(record_store=store, manifest=manifest)
            profile = store.read_product_profile_record("example-site")
            store.close()

        self.assertEqual(
            result.product_profile.historical_contexts,
            ("example-site-old", "example-site-preview-old"),
        )
        self.assertEqual(
            profile.historical_contexts,
            ("example-site-old", "example-site-preview-old"),
        )

    def test_apply_product_onboarding_manifest_keeps_existing_historical_contexts(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
            first_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T00:20:00Z",
            )
            store.write_product_profile_record(
                first_result.product_profile.model_copy(
                    update={
                        "historical_contexts": ("example-site-old",),
                        "updated_at": "2026-05-03T01:20:00Z",
                        "source": "test:cutover",
                    }
                )
            )

            second_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T02:20:00Z",
            )
            profile = store.read_product_profile_record("example-site")
            store.close()

        self.assertEqual(second_result.product_profile.historical_contexts, ("example-site-old",))
        self.assertEqual(profile.historical_contexts, ("example-site-old",))

    def test_apply_product_onboarding_manifest_preserves_configured_secret_binding(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
            store.write_secret_binding(
                SecretBinding(
                    binding_id="secret-runtime-environment-smtp-password-example-site-prod-prod-binding-smtp-password",
                    secret_id="secret-runtime-environment-smtp-password-example-site-prod-prod",
                    integration="runtime_environment",
                    binding_key="SMTP_PASSWORD",
                    context="example-site-prod",
                    instance="prod",
                    status="configured",
                    created_at="2026-05-03T00:30:00Z",
                    updated_at="2026-05-03T00:30:00Z",
                )
            )

            result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
            )

            secret_bindings = store.list_secret_bindings(
                integration="runtime_environment",
                context_name="example-site-prod",
                instance_name="prod",
            )
            store.close()

        self.assertEqual(result.secret_bindings, ())
        self.assertEqual(len(secret_bindings), 1)
        self.assertEqual(secret_bindings[0].binding_key, "SMTP_PASSWORD")
        self.assertEqual(secret_bindings[0].status, "configured")

    def test_apply_product_onboarding_manifest_retires_placeholder_when_context_binding_satisfies_instance(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
            first_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T00:20:00Z",
            )
            placeholder_binding_id = first_result.secret_bindings[0].binding_id
            store.write_secret_binding(
                SecretBinding(
                    binding_id="secret-runtime-environment-smtp-password-example-site-prod-binding-smtp-password",
                    secret_id="secret-runtime-environment-smtp-password-example-site-prod",
                    integration="runtime_environment",
                    binding_key="SMTP_PASSWORD",
                    context="example-site-prod",
                    instance="",
                    status="configured",
                    created_at="2026-05-03T00:30:00Z",
                    updated_at="2026-05-03T00:30:00Z",
                )
            )

            result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T02:30:00Z",
            )

            all_secret_bindings = store.list_secret_bindings(limit=None)
            active_secret_bindings = store.list_secret_bindings(
                integration="runtime_environment",
                context_name="example-site-prod",
                instance_name="prod",
            )
            store.close()

        retired_placeholder = next(
            binding
            for binding in all_secret_bindings
            if binding.binding_id == placeholder_binding_id
        )
        self.assertEqual(result.secret_bindings, ())
        self.assertEqual(retired_placeholder.integration, "retired:runtime_environment")
        self.assertEqual(retired_placeholder.status, "disabled")
        self.assertEqual(retired_placeholder.updated_at, "2026-05-03T02:30:00Z")
        self.assertEqual(active_secret_bindings, ())
        configured_bindings = [
            binding
            for binding in all_secret_bindings
            if binding.integration == "runtime_environment"
            and binding.binding_key == "SMTP_PASSWORD"
            and binding.context == "example-site-prod"
            and binding.instance == ""
        ]
        self.assertEqual(len(configured_bindings), 1)
        self.assertEqual(configured_bindings[0].status, "configured")

    def test_apply_product_onboarding_manifest_blocks_conflicting_provider_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="example-site-prod",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="stale-app-prod-123",
                    display_name="example-site-prod",
                    provider_target_type="application",
                    provider_evidence={"project_name": "example-site"},
                    updated_at="2026-05-03T00:00:00Z",
                    source_label="test:stale-provider-target",
                )
            )
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())

            with self.assertRaisesRegex(ValueError, "dual-write conflict"):
                apply_product_onboarding_manifest(record_store=store, manifest=manifest)

            self.assertEqual(store.list_dokploy_target_records(), ())
            self.assertEqual(store.list_dokploy_target_id_records(), ())
            store.close()

    def test_product_onboarding_manifest_prefers_provider_targets(self) -> None:
        payload = _manifest_payload()

        manifest = ProductOnboardingManifest.model_validate(payload)

        self.assertEqual(len(manifest.provider_targets), 2)
        self.assertEqual(
            [
                (target.context, target.instance, target.target_type, target.target_id)
                for target in manifest.provider_targets
            ],
            [
                ("example-site-testing", "testing", "application", "app-testing-123"),
                ("example-site-prod", "prod", "application", "app-prod-123"),
            ],
        )
        self.assertIn("provider_targets", manifest.model_dump())
        self.assertNotIn("dokploy_targets", manifest.model_dump())

    def test_product_onboarding_manifest_rejects_dokploy_targets_compat_input(
        self,
    ) -> None:
        payload = _manifest_payload()
        payload["dokploy_targets"] = json.loads(json.dumps(payload.pop("provider_targets")))

        with self.assertRaisesRegex(ValueError, "obsolete dokploy_targets"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_accepts_missing_provider_targets_as_empty(
        self,
    ) -> None:
        payload = _manifest_payload()
        payload.pop("provider_targets")

        manifest = ProductOnboardingManifest.model_validate(payload)

        self.assertEqual(manifest.provider_targets, ())

    def test_product_onboarding_manifest_keeps_empty_provider_targets_intentional(
        self,
    ) -> None:
        payload = _manifest_payload()
        payload["provider_targets"] = []

        manifest = ProductOnboardingManifest.model_validate(payload)

        self.assertEqual(manifest.provider_targets, ())

    def test_product_onboarding_manifest_rejects_unowned_target_route(self) -> None:
        payload = _manifest_payload()
        payload["provider_targets"] = [
            {
                "context": "other-product-prod",
                "instance": "prod",
                "target_id": "app-other-prod",
                "target_type": "application",
            }
        ]

        with self.assertRaisesRegex(ValueError, "target must match a stable lane"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_missing_target_id(self) -> None:
        payload = _manifest_payload()
        payload["provider_targets"] = [
            {
                "context": "example-site-prod",
                "instance": "prod",
                "target_id": "",
                "target_type": "application",
            }
        ]

        with self.assertRaisesRegex(ValueError, "target requires target_id"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_enabled_bootstrap_without_issue(
        self,
    ) -> None:
        payload = _manifest_payload()
        lanes = cast(list[dict[str, object]], payload["lanes"])
        first_lane = lanes[0]
        first_lane["odoo_stable_bootstrap"] = {
            "enabled": True,
            "confirmation": "bootstrap example testing",
            "expected_target_name": "example-site-testing",
            "expected_domains": ["testing.example.invalid"],
        }

        with self.assertRaisesRegex(ValueError, "approval_issue_url"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_duplicate_expected_config_keys(
        self,
    ) -> None:
        payload = _manifest_payload()
        payload["expected_config"] = {
            "runtime_environment_keys": [
                {"key": "PUBLIC_BASE_URL", "context": "example-site-prod", "instance": "prod"},
                {"key": "PUBLIC_BASE_URL", "context": "example-site-prod", "instance": "prod"},
            ]
        }

        with self.assertRaisesRegex(ValueError, "expected runtime config keys must be unique"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_cli_applies_manifest_without_secret_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            manifest_path = temporary_directory / "product-onboarding.json"
            manifest_path.write_text(json.dumps(_manifest_payload()))

            result = CliRunner().invoke(
                CLI_MAIN,
                [
                    "product-onboarding",
                    "apply",
                    "--database-url",
                    database_url,
                    "--manifest-file",
                    str(manifest_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["product"], "example-site")
        self.assertEqual(payload["secret_binding_count"], 1)
        self.assertNotIn("secret_id", payload["secret_bindings"][0])


if __name__ == "__main__":
    unittest.main()
