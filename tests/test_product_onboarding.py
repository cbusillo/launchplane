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
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest


CLI_MAIN = cast(Command, main)


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
        "dokploy_targets": [
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


class ProductOnboardingTests(unittest.TestCase):
    def test_deploy_authz_grants_include_scheduled_merge_train_runner(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("deploy:merge-train-runner-manual-grant", script_text)
        self.assertIn("merge-train-runner-manual", script_text)
        self.assertIn("deploy:merge-train-runner-schedule-grant", script_text)
        self.assertIn("merge-train-runner-schedule", script_text)
        self.assertIn("schedule", script_text)

    def test_deploy_authz_grants_include_reusable_odoo_stable_workflows(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(
            encoding="utf-8"
        )

        expected_workflows = (
            "reusable-odoo-artifact-publish.yml",
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
            self.assertIn(f"deploy:odoo-{context_name}-post-deploy-grant", script_text)
            self.assertIn(
                f"deploy:odoo-{context_name}-prod-promotion-run-grant", script_text
            )
            self.assertIn(f"deploy:odoo-{context_name}-prod-rollback-grant", script_text)

    def test_reusable_odoo_artifact_publish_standardizes_request_shape(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-odoo-artifact-publish.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_call", workflow_text)
        self.assertIn("/v1/drivers/odoo/artifact-publish-inputs", workflow_text)
        self.assertIn("/v1/drivers/odoo/artifact-publish", workflow_text)
        self.assertIn("odoo-artifact-publish-inputs:odoo:", workflow_text)
        self.assertIn("odoo-artifact-publish:odoo:", workflow_text)
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

    def test_reusable_odoo_prod_promotion_fails_on_each_result_status(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-odoo-prod-promotion.yml"
        ).read_text(encoding="utf-8")

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
                "case \"$*\" in\n"
                "  *github.invalid/oidc*) printf '{\"value\":\"oidc-token\"}' ;;\n"
                "  *)\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in\n"
                "        -o) shift; output_file=\"$1\" ;;\n"
                "        --data) shift; request_payload=\"$1\" ;;\n"
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q 'odoo-tenant-opw/.github/workflows/odoo-preview.yml'; then\n"
                "      printf '%s\\n%s\\n' \"$request_payload\" '---END-GRANT---' >> \"$CAPTURED_GRANTS\"\n"
                "    fi\n"
                "    if [ -n \"$output_file\" ]; then\n"
                "      printf '{\"status\":\"ok\"}' > \"$output_file\"\n"
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
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

    def test_deploy_authz_grants_include_terminal_agent_product_profile_read(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("product_profile.read", script_text)
        self.assertIn("deploy:terminal-agent-product-profile-read-grant", script_text)
        self.assertIn("terminal-agent-product-profile-read", script_text)

    def test_deploy_verireel_onboarding_manifest_enrolls_preview_lifecycle(
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
                "case \"$*\" in\n"
                "  *github.invalid/oidc*) printf '{\"value\":\"oidc-token\"}' ;;\n"
                "  *)\n"
                "    idempotency_key=''\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in\n"
                "        -o) shift; output_file=\"$1\" ;;\n"
                "        -H)\n"
                "          shift\n"
                "          case \"$1\" in\n"
                "            Idempotency-Key:*) idempotency_key=\"$1\" ;;\n"
                "          esac\n"
                "          ;;\n"
                "        --data) shift; request_payload=\"$1\" ;;\n"
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    case \"$idempotency_key\" in\n"
                "      *launchplane-product-onboarding:verireel-preview-profile*)\n"
                "        printf '%s' \"$request_payload\" > \"$CAPTURED_REQUEST_PAYLOAD\"\n"
                "        printf '%s' \"${idempotency_key#Idempotency-Key: }\" > \"$CAPTURED_IDEMPOTENCY_KEY\"\n"
                "        ;;\n"
                "    esac\n"
                "    if [ -n \"$output_file\" ]; then\n"
                "      printf '{\"status\":\"ok\"}' > \"$output_file\"\n"
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_request_payload = temporary_directory / "request.json"
            captured_idempotency_key = temporary_directory / "idempotency-key.txt"
            captured_idempotency_key.touch()
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
                "CAPTURED_REQUEST_PAYLOAD": str(captured_request_payload),
                "CAPTURED_IDEMPOTENCY_KEY": str(captured_idempotency_key),
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
            request_payload = json.loads(captured_request_payload.read_text())
            idempotency_key = captured_idempotency_key.read_text().strip()

        manifest = ProductOnboardingManifest.model_validate(request_payload["manifest"])

        self.assertEqual(request_payload["product"], "launchplane")
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
        self.assertEqual(len(manifest.dokploy_targets), 0)
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
        self.assertEqual(
            idempotency_key,
            "launchplane-product-onboarding:verireel-preview-profile:test-sha",
        )

    def test_deploy_odoo_cm_onboarding_manifest_encodes_issue_backed_bootstrap_policy(
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
                "case \"$*\" in\n"
                "  *github.invalid/oidc*) printf '{\"value\":\"oidc-token\"}' ;;\n"
                "  *)\n"
                "    idempotency_key=''\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in\n"
                "        -o)\n"
                "          shift\n"
                "          output_file=\"$1\"\n"
                "          ;;\n"
                "        -H)\n"
                "          shift\n"
                "          case \"$1\" in\n"
                "            Idempotency-Key:*) idempotency_key=\"$1\" ;;\n"
                "          esac\n"
                "          ;;\n"
                "        --data)\n"
                "          shift\n"
                "          request_payload=\"$1\"\n"
                "          ;;\n"
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    case \"$idempotency_key\" in\n"
                "      *launchplane-product-onboarding:odoo-cm-preview-profile*)\n"
                "        printf '%s' \"$request_payload\" > \"$CAPTURED_REQUEST_PAYLOAD\"\n"
                "        printf '%s' \"${idempotency_key#Idempotency-Key: }\" > \"$CAPTURED_IDEMPOTENCY_KEY\"\n"
                "        ;;\n"
                "    esac\n"
                "    if [ -n \"$output_file\" ]; then\n"
                "      printf '{\"status\":\"ok\"}' > \"$output_file\"\n"
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_curl_args = temporary_directory / "curl-args.txt"
            captured_idempotency_key = temporary_directory / "idempotency-key.txt"
            captured_idempotency_key.touch()
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
                "CAPTURED_REQUEST_PAYLOAD": str(captured_curl_args),
                "CAPTURED_IDEMPOTENCY_KEY": str(captured_idempotency_key),
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
            request_payload = json.loads(captured_curl_args.read_text())
            idempotency_key = captured_idempotency_key.read_text().strip()

        manifest = ProductOnboardingManifest.model_validate(request_payload["manifest"])

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
            [tuple(target.domains) for target in manifest.dokploy_targets],
            [
                ("cm-testing.shinycomputers.com",),
                ("cm-prod.shinycomputers.com",),
            ],
        )
        self.assertEqual(
            [
                (target.context, target.instance, target.target_type, target.target_id)
                for target in manifest.dokploy_targets
            ],
            [
                ("cm", "testing", "compose", "compose-cm-testing"),
                ("cm", "prod", "compose", "compose-cm-prod"),
            ],
        )
        self.assertEqual(
            idempotency_key,
            "launchplane-product-onboarding:odoo-cm-preview-profile:compose-cm-testing:compose-cm-prod:test-sha",
        )

    def test_deploy_odoo_cm_onboarding_manifest_requires_prod_target_id(self) -> None:
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
                "case \"$*\" in\n"
                "  *github.invalid/oidc*) printf '{\"value\":\"oidc-token\"}' ;;\n"
                "  *) printf '200' ;;\n"
                "esac\n"
                "output_file=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -o)\n"
                "      shift\n"
                "      output_file=\"$1\"\n"
                "      ;;\n"
                "  esac\n"
                "  shift || true\n"
                "done\n"
                "if [ -n \"$output_file\" ]; then\n"
                "  printf '{\"status\":\"ok\"}' > \"$output_file\"\n"
                "fi\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
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
                "DISCORD_BLUE_DOKPLOY_TARGET_ID": "app-discord-blue",
                "ODOO_CM_TESTING_DOKPLOY_TARGET_ID": "compose-cm-testing",
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
        self.assertIn("ODOO_CM_PROD_DOKPLOY_TARGET_ID is required", result.stderr)

    def test_deploy_odoo_opw_onboarding_manifest_encodes_upstream_restore_policy(
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
                "case \"$*\" in\n"
                "  *github.invalid/oidc*) printf '{\"value\":\"oidc-token\"}' ;;\n"
                "  *)\n"
                "    idempotency_key=''\n"
                "    output_file=''\n"
                "    request_payload=''\n"
                "    while [ \"$#\" -gt 0 ]; do\n"
                "      case \"$1\" in\n"
                "        -o) shift; output_file=\"$1\" ;;\n"
                "        -H)\n"
                "          shift\n"
                "          case \"$1\" in\n"
                "            Idempotency-Key:*) idempotency_key=\"$1\" ;;\n"
                "          esac\n"
                "          ;;\n"
                "        --data) shift; request_payload=\"$1\" ;;\n"
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    case \"$idempotency_key\" in\n"
                "      *launchplane-product-onboarding:odoo-opw-prelaunch-profile*)\n"
                "        printf '%s' \"$request_payload\" > \"$CAPTURED_REQUEST_PAYLOAD\"\n"
                "        ;;\n"
                "    esac\n"
                "    if [ -n \"$output_file\" ]; then\n"
                "      printf '{\"status\":\"ok\"}' > \"$output_file\"\n"
                "    fi\n"
                "    printf '200'\n"
                "    ;;\n"
                "esac\n"
            )
            (captured_bin_directory / "mktemp").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$CAPTURED_RESPONSE_FILE\"\n"
            )
            (captured_bin_directory / "curl").chmod(0o755)
            (captured_bin_directory / "mktemp").chmod(0o755)
            captured_request_payload = temporary_directory / "request.json"
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
                "CAPTURED_REQUEST_PAYLOAD": str(captured_request_payload),
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
            request_payload = json.loads(captured_request_payload.read_text())

        manifest = ProductOnboardingManifest.model_validate(request_payload["manifest"])

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
            )

            profile = store.read_product_profile_record("example-site")
            targets = store.list_dokploy_target_records()
            target_ids = store.list_dokploy_target_id_records()
            runtime_records = store.list_runtime_environment_records()
            secret_bindings = store.list_secret_bindings()
            store.close()

        self.assertEqual(first_result.product, "example-site")
        self.assertEqual(second_result.product_profile.updated_at, "2026-05-03T01:30:00Z")
        self.assertEqual(profile.driver_id, "generic-web")
        self.assertEqual(profile.lanes[0].health_url, "https://testing.example.invalid/api/health")
        self.assertTrue(profile.lanes[0].odoo_stable_bootstrap.enabled)
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
        self.assertEqual(profile.lanes[1].odoo_data_policy.upstream_source, "example-site/prod/upstream")
        self.assertEqual(profile.preview.enable_label, "preview-requested")
        self.assertEqual(profile.expected_config.runtime_environment_keys[0].key, "PUBLIC_BASE_URL")
        self.assertEqual(
            profile.expected_config.managed_secret_bindings[0].binding_key,
            "SMTP_PASSWORD",
        )
        self.assertEqual(len(targets), 2)
        self.assertEqual(len(target_ids), 2)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(len(secret_bindings), 1)
        self.assertEqual(secret_bindings[0].binding_key, "SMTP_PASSWORD")
        self.assertEqual(secret_bindings[0].status, "disabled")

    def test_product_onboarding_manifest_rejects_unowned_target_route(self) -> None:
        payload = _manifest_payload()
        payload["dokploy_targets"] = [
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
        payload["dokploy_targets"] = [
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
