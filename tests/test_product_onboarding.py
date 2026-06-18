import json
import os
import importlib.util
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Callable, cast
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
_HEALTH_MONITORING_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "control_plane"
    / "storage"
    / "migrations"
    / "versions"
    / "fa2c4e6f8a0b_migrate_lane_health_monitoring.py"
)
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
OWNER_AUTHZ_ENV = {
    "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT": "test-terminal-agent-subject",
    "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL": "test-terminal-agent-read-token",
    "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT": "test-local-operator-subject",
    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL": "test-local-operator-write-token",
    "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT": "test-local-admin-subject",
    "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL": "test-local-admin-token",
}


def _load_health_monitoring_migration() -> object:
    spec = importlib.util.spec_from_file_location(
        "launchplane_health_monitoring_migration", _HEALTH_MONITORING_MIGRATION_PATH
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
                "health_monitoring": {
                    "checks": [
                        {
                            "name": "public-ingress",
                            "kind": "public_http",
                        }
                    ]
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
    def test_deploy_authz_grants_include_scheduled_merge_train_runner(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("deploy:merge-train-runner-manual-grant", script_text)
        self.assertIn("merge-train-runner-manual", script_text)
        self.assertIn("merge_train.policy_targets", script_text)
        self.assertIn("deploy:merge-train-runner-policy-targets-schedule-grant", script_text)
        self.assertIn("merge-train-runner-policy-targets-schedule", script_text)
        self.assertIn("deploy:merge-train-runner-schedule-grant", script_text)
        self.assertIn("merge-train-runner-schedule", script_text)
        self.assertIn("schedule", script_text)

    def test_deploy_authz_grants_include_runner_registration_audit_writer(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("runner-lane-registration.yml", script_text)
        self.assertIn("runner_lane_registration_audit.write", script_text)
        self.assertIn("deploy:runner-lane-registration-audit-grant", script_text)
        self.assertIn("runner-lane-registration-audit", script_text)

    def test_deploy_authz_grants_accept_configured_github_action_grants(
        self,
    ) -> None:
        script_path = Path("scripts/deploy/ensure-authz-grants.sh")
        extractor = """
set -euo pipefail
PATH="$CAPTURED_BIN_DIR:$PATH" bash scripts/deploy/ensure-authz-grants.sh
"""
        configured_grants = [
            {
                "repository": "example-org/example-product",
                "workflow_file": "deploy.yml",
                "product": "example-product",
                "context": "example-context",
                "action": "example_action.execute",
                "source_label": "operator-config:example-product-deploy",
                "idempotency_suffix": "example-product-deploy",
                "event_name": "push",
                "workflow_ref_suffix": "refs/heads/release",
                "job_workflow_ref": (
                    "example-org/launchplane/.github/workflows/reusable.yml@refs/heads/main"
                ),
            }
        ]
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            captured_bin_directory = temporary_directory / "bin"
            captured_bin_directory.mkdir()
            (captured_bin_directory / "curl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *github.invalid/oidc*) printf \'{"value":"oidc-token"}\' ;;\n'
                "  *)\n"
                '    printf \'%s\\n\' "$*" >> "$CAPTURED_GRANT_ATTEMPTS"\n'
                "    output_file=''\n"
                "    request_payload=''\n"
                '    while [ "$#" -gt 0 ]; do\n'
                '      case "$1" in\n'
                '        -o) shift; output_file="$1" ;;\n'
                '        --data) shift; request_payload="$1" ;;\n'
                "      esac\n"
                "      shift || true\n"
                "    done\n"
                "    if printf '%s' \"$request_payload\" | grep -q 'example-product-deploy'; then\n"
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
                **OWNER_AUTHZ_ENV,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_AUTHZ_GRANTS_JSON": json.dumps(configured_grants),
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

        self.assertEqual(len(grants), 1)
        grant = grants[0]
        self.assertEqual(grant["repository"], "example-org/example-product")
        self.assertEqual(
            grant["workflow_refs"],
            ["example-org/example-product/.github/workflows/deploy.yml@refs/heads/release"],
        )
        self.assertEqual(
            grant["job_workflow_refs"],
            ["example-org/launchplane/.github/workflows/reusable.yml@refs/heads/main"],
        )
        self.assertEqual(grant["event_names"], ["push"])
        self.assertEqual(grant["products"], ["example-product"])
        self.assertEqual(grant["contexts"], ["example-context"])
        self.assertEqual(grant["actions"], ["example_action.execute"])
        self.assertEqual(grant["source_label"], "operator-config:example-product-deploy")

    def test_deploy_authz_grants_require_configured_owner_identities(
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
                '    printf \'%s\\n\' "$*" >> "$CAPTURED_GRANT_ATTEMPTS"\n'
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
            captured_grant_attempts = temporary_directory / "grant-attempts.txt"
            env = {key: value for key, value in os.environ.items() if key not in OWNER_AUTHZ_ENV}
            env.update(
                {
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                    "GITHUB_REPOSITORY": "cbusillo/launchplane",
                    "GITHUB_SHA": "test-sha",
                    "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                    "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                    "CAPTURED_RESPONSE_FILE": str(captured_response_file),
                    "CAPTURED_GRANT_ATTEMPTS": str(captured_grant_attempts),
                    "CAPTURED_BIN_DIR": str(captured_bin_directory),
                }
            )

            result = subprocess.run(
                ["bash", "-c", extractor],
                check=False,
                cwd=script_path.parent.parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Configured authz grant is missing LAUNCHPLANE_TERMINAL_AGENT_SUBJECT.",
            result.stderr,
        )
        self.assertFalse(captured_grant_attempts.exists())

    def test_reusable_odoo_artifact_publish_standardizes_request_shape(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-artifact-publish.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_call", workflow_text)
        self.assertIn("workflow_dispatch:", workflow_text)
        self.assertIn("product:", workflow_text)
        self.assertIn("product_repository:", workflow_text)
        self.assertIn("source_git_ref:", workflow_text)
        self.assertIn("PUBLISH LAUNCHPLANE ODOO ARTIFACT", workflow_text)
        self.assertIn("product is required.", workflow_text)
        self.assertNotIn('context_slug="${CONTEXT_NAME//_/-}"', workflow_text)
        self.assertNotIn('product="odoo-tenant-${context_slug}"', workflow_text)
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
        self.assertIn("repository: ${{ steps.source.outputs.repository }}", workflow_text)
        self.assertIn("ref: ${{ steps.source.outputs.source_git_ref }}", workflow_text)
        self.assertIn(
            "token: ${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",
            workflow_text,
        )
        self.assertIn(
            "inputs.source_git_ref=${{ steps.source.outputs.source_git_ref }}",
            workflow_text,
        )
        self.assertIn(
            "${{ secrets.ODOO_SOURCE_GITHUB_TOKEN || github.token }}",
            workflow_text,
        )
        self.assertIn("CONTEXT_NAME: ${{ inputs.context }}", workflow_text)
        self.assertIn('output_file="$RUNNER_TEMP/${CONTEXT_NAME}-artifact.json"', workflow_text)
        self.assertIn(
            "RESOLVED_IMAGE_REPOSITORY: >-\n"
            "            ${{ steps.publish_inputs.outputs.image_repository }}",
            workflow_text,
        )
        self.assertIn(
            "RESOLVED_IMAGE_TAG: ${{ steps.publish_inputs.outputs.image_tag }}",
            workflow_text,
        )
        self.assertIn(
            "RESOLVED_DEVKIT_REPOSITORY: >-\n"
            "            ${{ steps.publish_inputs.outputs.devkit_repository }}",
            workflow_text,
        )
        self.assertIn(
            "RESOLVED_SHARED_ADDONS_REPOSITORY: >-\n"
            "            ${{ steps.publish_inputs.outputs.shared_addons_repository }}",
            workflow_text,
        )
        self.assertIn("devkit_repository=result.devkit_repository", workflow_text)
        self.assertIn(
            "shared_addons_repository=result.shared_addons_repository",
            workflow_text,
        )
        self.assertIn(
            "repository: ${{ steps.publish_inputs.outputs.devkit_repository }}", workflow_text
        )
        self.assertIn(
            "repository: ${{ steps.publish_inputs.outputs.shared_addons_repository }}",
            workflow_text,
        )
        self.assertIn("publish.manifest=${{ steps.publish.outputs.manifest_file }}", workflow_text)
        self.assertNotIn("short_sha=", workflow_text)
        self.assertNotIn("IMAGE_REPOSITORY: ghcr.io/${{ github.repository }}", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-devkit", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-shared-addons", workflow_text)

    def test_reusable_odoo_testing_deploy_requires_explicit_product_scope(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-testing-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("/v1/drivers/odoo/target-replacement-apply", workflow_text)
        self.assertIn("product is required.", workflow_text)
        self.assertNotIn('context_slug="${CONTEXT_NAME//_/-}"', workflow_text)
        self.assertNotIn('product="odoo-tenant-${context_slug}"', workflow_text)
        self.assertIn("product=${{ steps.product.outputs.product }}", workflow_text)
        self.assertIn('"instance": "testing"', workflow_text)
        self.assertIn("replacement.artifact_id=${{ inputs.artifact_id }}", workflow_text)
        self.assertIn("${{ steps.product.outputs.idempotency_key }}", workflow_text)

    def test_reusable_odoo_post_deploy_requires_explicit_product_scope(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-post-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("/v1/drivers/odoo/post-deploy", workflow_text)
        self.assertIn("product is required.", workflow_text)
        self.assertNotIn('product="odoo-tenant-${context_slug}"', workflow_text)
        self.assertNotIn('context_slug="${CONTEXT_NAME//_/-}"', workflow_text)
        self.assertIn("product=${{ steps.product.outputs.product }}", workflow_text)
        self.assertIn("${{ steps.product.outputs.idempotency_key }}", workflow_text)
        self.assertIn("website_bootstrap_included=result.website_bootstrap_included", workflow_text)
        self.assertNotIn('"product":"odoo"', workflow_text)

    def test_reusable_odoo_prod_workflows_require_explicit_product_scope(self) -> None:
        workflow_paths = (
            Path(".github/workflows/reusable-odoo-prod-promotion.yml"),
            Path(".github/workflows/reusable-odoo-prod-rollback.yml"),
        )

        for workflow_path in workflow_paths:
            with self.subTest(workflow=workflow_path.name):
                workflow_text = workflow_path.read_text(encoding="utf-8")
                self.assertIn("product is required.", workflow_text)
                self.assertNotIn('context_slug="${CONTEXT_NAME//_/-}"', workflow_text)
                self.assertNotIn('product="odoo-tenant-${context_slug}"', workflow_text)
                self.assertIn("product=${{ steps.product.outputs.product }}", workflow_text)
                self.assertIn("${{ steps.product.outputs.idempotency_key }}", workflow_text)
                self.assertNotIn('"product":"odoo"', workflow_text)

    def test_reusable_odoo_workflows_use_caller_visible_runner(self) -> None:
        workflow_paths = (
            Path(".github/workflows/reusable-odoo-artifact-publish.yml"),
            Path(".github/workflows/reusable-odoo-testing-deploy.yml"),
            Path(".github/workflows/reusable-odoo-post-deploy.yml"),
            Path(".github/workflows/reusable-odoo-prod-promotion.yml"),
            Path(".github/workflows/reusable-odoo-prod-rollback.yml"),
        )

        for workflow_path in workflow_paths:
            with self.subTest(workflow=workflow_path.name):
                workflow_text = workflow_path.read_text(encoding="utf-8")
                self.assertIn("runs-on: ubuntu-latest", workflow_text)
                self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
                self.assertNotIn("runs-on:\n      - self-hosted", workflow_text)

    def test_odoo_website_bootstrap_override_requires_explicit_target_inputs(self) -> None:
        workflow_text = Path(".github/workflows/odoo-website-bootstrap-override.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("PRODUCT: ${{ inputs.product }}", workflow_text)
        self.assertIn("CONTEXT_NAME: ${{ inputs.context }}", workflow_text)
        self.assertIn("INSTANCE: ${{ inputs.instance }}", workflow_text)
        self.assertIn("          - prod", workflow_text)
        self.assertNotIn("allowed_targets=", workflow_text)
        self.assertNotIn("odoo-tenant-opw:opw:testing", workflow_text)
        self.assertNotIn("odoo-tenant-opw:opw:prod", workflow_text)
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

    def test_route_batch_workflows_require_configured_json_routes(self) -> None:
        workflow_expectations = {
            "provider-target-operations.yml": (
                "target_set=configured-json",
                'JSON array of {"context","instance"} routes',
            ),
            "product-environment-evidence.yml": (
                "target_set=configured-json",
                'JSON array of {"product","environment"} routes',
            ),
        }
        forbidden_literals = (
            "phase-two-initial",
            "discord-blue",
            "sellyouroutboard",
            "verireel",
            "odoo-tenant-cm",
            "odoo-tenant-opw",
            '"cm"',
            '"opw"',
        )

        for workflow_name, expected_snippets in workflow_expectations.items():
            workflow_text = Path(f".github/workflows/{workflow_name}").read_text(encoding="utf-8")
            with self.subTest(workflow=workflow_name):
                self.assertIn("routes_json:", workflow_text)
                self.assertIn("configured-json", workflow_text)
                self.assertIn("ROUTES_JSON", workflow_text)
                for snippet in expected_snippets:
                    self.assertIn(snippet, workflow_text)
                for literal in forbidden_literals:
                    self.assertNotIn(literal, workflow_text)

    def test_dokploy_target_setup_workflow_supports_compose_domain_reconcile(self) -> None:
        workflow_text = Path(".github/workflows/dokploy-target-setup.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("- reconcile-compose-domain", workflow_text)
        self.assertIn("DOMAIN: ${{ inputs.domain }}", workflow_text)
        self.assertIn("RUNTIME_PORT: ${{ inputs.runtime_port }}", workflow_text)
        self.assertIn("Validate reconcile compose domain inputs", workflow_text)
        self.assertIn("domain is required for compose domain reconcile/prune", workflow_text)
        self.assertIn("runtime_port is required for reconcile-compose-domain", workflow_text)
        self.assertIn("expected_current_provider_target_json:", workflow_text)
        self.assertIn("EXPECTED_CURRENT_PROVIDER_TARGET_JSON", workflow_text)
        self.assertIn("expected_current_provider_target", workflow_text)
        self.assertIn("APPLY DOKPLOY TARGET SETUP", workflow_text)

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

    def test_odoo_driver_route_smoke_proves_public_and_oidc_paths(self) -> None:
        workflow_text = Path(".github/workflows/odoo-driver-route-smoke.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_call:", workflow_text)
        self.assertIn("id-token: write", workflow_text)
        self.assertIn("LAUNCHPLANE_PUBLIC_URL", workflow_text)
        self.assertIn("source_git_ref:", workflow_text)
        self.assertIn("SOURCE_GIT_REF: ${{ inputs.source_git_ref }}", workflow_text)
        self.assertIn('[ "$status_code" = "404" ]', workflow_text)
        self.assertIn('[ "$status_code" -ge 500 ]', workflow_text)
        self.assertIn("--connect-timeout 10", workflow_text)
        self.assertIn("--max-time 30", workflow_text)
        self.assertIn("${GITHUB_RUN_ID}", workflow_text)
        self.assertIn("${GITHUB_RUN_ATTEMPT}", workflow_text)
        self.assertIn("uses: actions/checkout@v6", workflow_text)
        self.assertIn("uses: ./.github/actions/launchplane-request", workflow_text)
        self.assertIn("route-path: /v1/drivers/odoo/artifact-publish-inputs", workflow_text)
        self.assertIn("Classify authenticated Odoo route responses", workflow_text)
        self.assertIn("ROUTE_PROBES", workflow_text)
        self.assertIn("/v1/drivers/odoo/preview-apply-inputs", workflow_text)
        self.assertIn("/v1/drivers/odoo/preview-apply", workflow_text)
        self.assertIn("/v1/previews/pr-feedback", workflow_text)
        self.assertIn("Authorization: Bearer ${oidc_token}", workflow_text)
        self.assertIn("401)", workflow_text)
        self.assertIn("rejected the GitHub OIDC token", workflow_text)
        self.assertIn("404)", workflow_text)
        self.assertIn("is not registered on Launchplane", workflow_text)
        self.assertIn('"expected_statuses": ["202", "403", "503"]', workflow_text)
        self.assertIn('"expected_statuses": ["202"]', workflow_text)
        self.assertIn('"dry_run": true', workflow_text)
        self.assertIn("'.result.preview_pr_feedback // \"\"'", workflow_text)
        self.assertIn("authorized", workflow_text)
        self.assertIn("accepted a non-dry-run feedback probe", workflow_text)
        self.assertIn('--data-urlencode "audience=${service_audience}"', workflow_text)
        self.assertIn(
            "Artifact publish inputs did not include the product repository", workflow_text
        )
        self.assertIn("PRODUCT_REPOSITORY", workflow_text)
        self.assertIn("source_repository=result.repository", workflow_text)
        self.assertIn('"operation": "destroy"', workflow_text)
        self.assertIn('"code": "runtime_plan_not_ready"', workflow_text)
        self.assertIn("accepted a non-blocked apply probe", workflow_text)
        self.assertIn("'.result.status // \"\"'", workflow_text)
        self.assertIn("error_code", workflow_text)
        self.assertIn("trace_id", workflow_text)
        self.assertIn("product=${{ env.PRODUCT }}", workflow_text)
        self.assertIn("inputs.context=${{ env.CONTEXT_NAME }}", workflow_text)
        self.assertIn("inputs.instance=${{ env.INSTANCE }}", workflow_text)
        self.assertIn("inputs.source_git_ref=${{ env.SOURCE_GIT_REF }}", workflow_text)
        self.assertIn("image_repository=result.image_repository", workflow_text)
        self.assertIn("image_tag=result.image_tag", workflow_text)
        self.assertIn('[ -z "$IMAGE_REPOSITORY" ]', workflow_text)
        self.assertIn('[ -z "$IMAGE_TAG" ]', workflow_text)
        self.assertNotIn("input_status", workflow_text)

    def test_ingress_route_dry_run_workflow_rejects_non_object_options(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-dry-run.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('($options | type) != "object"', workflow_text)
        self.assertIn('error("route_options_json must be a JSON object")', workflow_text)
        self.assertIn("expected_host_id:", workflow_text)
        self.assertIn("Optional existing provider host id", workflow_text)
        self.assertIn("EXPECTED_HOST_ID: ${{ inputs.expected_host_id }}", workflow_text)
        self.assertIn('--arg expected_host_id "$EXPECTED_HOST_ID"', workflow_text)
        self.assertIn('if . == "" then', workflow_text)
        self.assertIn('elif . == "null" then', workflow_text)
        self.assertIn(
            'error("expected_host_id must be blank or a JSON number")',
            workflow_text,
        )
        self.assertIn("expected_host_id: $expected_host_id_value", workflow_text)
        self.assertIn(
            'error("expected_host_id must be a number when provided")',
            workflow_text,
        )
        self.assertIn(
            'error("forward_host and edge_endpoint_key are mutually exclusive")',
            workflow_text,
        )
        self.assertIn(
            'error("route.forward_host or route.edge_endpoint_key is required")',
            workflow_text,
        )
        self.assertIn(
            'error("route.forward_host and route.edge_endpoint_key are mutually exclusive")',
            workflow_text,
        )
        self.assertIn(
            'error("route_options_json contains unsupported route option key(s)")',
            workflow_text,
        )
        self.assertIn("forward_host: $forward_host", workflow_text)
        self.assertIn("edge_endpoint_key: $edge_endpoint_key", workflow_text)
        self.assertNotIn('                "forward_host",', workflow_text)
        self.assertNotIn('                "edge_endpoint_key",', workflow_text)
        inputs_section = workflow_text.split("permissions:", maxsplit=1)[0]
        self.assertEqual(inputs_section.count("        description:"), 11)
        self.assertIn("edge_endpoint_key:", inputs_section)
        self.assertIn('default: ""', inputs_section)
        self.assertNotIn("identity_access_provider:", inputs_section)
        self.assertNotIn("identity_access_send_basic_auth:", inputs_section)
        self.assertIn("uses: actions/checkout@v6", workflow_text)
        self.assertIn("uses: ./.github/actions/launchplane-request", workflow_text)
        self.assertIn("PRODUCT: ${{ inputs.product }}", workflow_text)
        self.assertIn("CONTEXT: ${{ inputs.context }}", workflow_text)
        self.assertIn("DOMAIN: ${{ inputs.domain }}", workflow_text)
        self.assertIn("Existing provider certificate id, or new", workflow_text)
        self.assertIn('--arg certificate_id "$CERTIFICATE_ID"', workflow_text)
        self.assertIn('if $certificate_id_text == "new" then', workflow_text)
        self.assertIn('"certificate_id must be an integer or new"', workflow_text)
        self.assertIn("certificate_id: $certificate_id_value", workflow_text)
        self.assertIn('echo "- Product: $PRODUCT"', workflow_text)
        self.assertIn('echo "- Context: $CONTEXT"', workflow_text)
        self.assertIn('echo "- Domain: $DOMAIN"', workflow_text)
        self.assertNotIn('echo "- Product: ${{ inputs.product }}"', workflow_text)
        self.assertNotIn('echo "- Context: ${{ inputs.context }}"', workflow_text)
        self.assertNotIn('echo "- Domain: ${{ inputs.domain }}"', workflow_text)
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
        self.assertNotIn("launchplane-request", workflow_text)
        self.assertNotIn("ingress_route.apply", workflow_text)
        self.assertNotIn("provider_host_id:", workflow_text)
        self.assertNotIn("idempotency-key:", workflow_text)

    def test_ingress_route_canary_apply_workflow_requires_apply_guards(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-canary-apply.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("idempotency-key: ${{ inputs.idempotency_key }}", workflow_text)
        self.assertIn("CONFIRMATION: ${{ inputs.confirmation }}", workflow_text)
        self.assertIn("apply ingress canary", workflow_text)
        self.assertIn("canary_key:", workflow_text)
        self.assertIn("CANARY_KEY: ${{ inputs.canary_key }}", workflow_text)
        self.assertIn("canary_key: $canary_key", workflow_text)
        self.assertIn("route-path: /v1/ingress/canary-routes/apply", workflow_text)
        inputs_section = workflow_text.split("permissions:", maxsplit=1)[0]
        self.assertNotIn("      domain:", inputs_section)
        self.assertNotIn("      expected_host_id:", inputs_section)
        self.assertNotIn("      edge_endpoint_key:", inputs_section)
        self.assertNotIn("LAUNCHPLANE_INGRESS_CANARY", workflow_text)
        self.assertNotIn("CANARY_FORWARD_HOST", workflow_text)
        self.assertNotIn("CANARY_FORWARD_PORT", workflow_text)
        self.assertNotIn("CANARY_DOMAIN", workflow_text)
        self.assertNotIn("CANARY_EXPECTED_HOST_ID", workflow_text)
        self.assertNotIn("CANARY_CERTIFICATE_ID", workflow_text)
        self.assertNotIn("forward_host: $forward_host", workflow_text)
        self.assertNotIn("forward_port: $forward_port", workflow_text)
        self.assertIn("categories=\\($categories)", workflow_text)
        self.assertNotIn("route_options_json", workflow_text)

    def test_ingress_route_apply_workflow_requires_operator_guards(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-apply.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('mode: "apply"', workflow_text)
        self.assertIn("idempotency-key: ${{ inputs.idempotency_key }}", workflow_text)
        self.assertIn("CONFIRMATION: ${{ inputs.confirmation }}", workflow_text)
        self.assertIn("APPLY LAUNCHPLANE INGRESS ROUTE", workflow_text)
        self.assertIn("route_json:", workflow_text)
        self.assertIn("route.edge_endpoint_key", workflow_text)
        self.assertIn("route_json.route.domain_names must be non-empty", workflow_text)
        self.assertIn('option("allow_create"; false) as $allow_create', workflow_text)
        self.assertIn("($input.expected_host_id // null) as $expected_host_id", workflow_text)
        self.assertIn('($allow_create | type) != "boolean"', workflow_text)
        self.assertIn("route_json.allow_create must be a boolean", workflow_text)
        self.assertNotIn("$expected_host_id == null", workflow_text)
        self.assertNotIn("unless allow_create is true", workflow_text)
        self.assertIn(
            "route_json.expected_host_id must be a number or null",
            workflow_text,
        )
        self.assertIn("expected_host_id: $expected_host_id", workflow_text)
        self.assertIn("allow_create: $allow_create", workflow_text)
        self.assertIn('allow_update: option("allow_update"; true)', workflow_text)
        self.assertIn('allow_enable_disable: option("allow_enable_disable"; false)', workflow_text)
        self.assertIn("uses: ./.github/actions/launchplane-request", workflow_text)
        self.assertIn("route-path: /v1/drivers/ingress/route-apply", workflow_text)
        self.assertIn("PRODUCT: ${{ inputs.product }}", workflow_text)
        self.assertIn("CONTEXT: ${{ inputs.context }}", workflow_text)
        self.assertIn('echo "- Product: $PRODUCT"', workflow_text)
        self.assertIn('echo "- Context: $CONTEXT"', workflow_text)
        self.assertNotIn('echo "- Product: ${{ inputs.product }}"', workflow_text)
        self.assertNotIn('echo "- Context: ${{ inputs.context }}"', workflow_text)
        inputs_section = workflow_text.split("permissions:", maxsplit=1)[0]
        self.assertEqual(inputs_section.count("        description:"), 6)

    def test_github_metadata_prefers_repository_merge_method(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["pullRequests"]["preferredMergeMethod"], "merge")
        self.assertEqual(metadata["pullRequests"]["allowedMergeMethods"], ["merge"])
        self.assertIn("Ingress Route Apply", metadata["importantWorkflows"])
        self.assertNotIn("healthUrls", metadata)

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

    def test_deploy_launchplane_requires_compose_network_for_compose_targets(
        self,
    ) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn(
            "Missing LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK variable",
            workflow_text,
        )
        self.assertIn(
            'if $compose_external_network != "" then\n'
            "                      {LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK: $compose_external_network}",
            workflow_text,
        )
        self.assertNotIn("omit_compose_external_network_env", workflow_text)

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

    def test_deploy_launchplane_projects_public_ingress_github_token(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn(
            "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN: "
            "${{ secrets.LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN }}",
            workflow_text,
        )
        self.assertIn(
            '--arg public_ingress_github_token "${LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN:-}"',
            workflow_text,
        )
        self.assertIn(
            "{LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN: $public_ingress_github_token}",
            workflow_text,
        )

        removals_block = workflow_text.split("service_env_removals_json=", 1)[1].split(
            '            })"', 1
        )[0]
        jq_filter = removals_block.split("                '", 1)[1].rsplit("'", 1)[0]
        self.assertIn("$public_ingress_github_token", jq_filter)
        self.assertIn("LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN", jq_filter)

        def evaluate_removals(
            *, public_ingress_github_token: str, omit_npmplus_env: bool
        ) -> list[str]:
            result = subprocess.run(
                [
                    "jq",
                    "-n",
                    "--arg",
                    "public_ingress_github_token",
                    public_ingress_github_token,
                    "--argjson",
                    "omit_npmplus_env",
                    json.dumps(omit_npmplus_env),
                    jq_filter,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return cast(list[str], json.loads(result.stdout))

        self.assertEqual(
            evaluate_removals(public_ingress_github_token="", omit_npmplus_env=False),
            ["LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN"],
        )
        self.assertEqual(
            evaluate_removals(
                public_ingress_github_token="public-ingress-token",
                omit_npmplus_env=False,
            ),
            [],
        )
        self.assertEqual(
            evaluate_removals(public_ingress_github_token="", omit_npmplus_env=True),
            [
                "LAUNCHPLANE_NPMPLUS_BASE_URL",
                "LAUNCHPLANE_NPMPLUS_IDENTITY",
                "LAUNCHPLANE_NPMPLUS_SECRET",
                "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN",
            ],
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

    def test_product_onboarding_workflow_calls_service_route_with_apply_guard(self) -> None:
        workflow_text = Path(".github/workflows/product-onboarding.yml").read_text(encoding="utf-8")

        self.assertIn("/v1/product-onboarding/apply", workflow_text)
        self.assertIn("APPLY PRODUCT ONBOARDING", workflow_text)
        self.assertIn("manifest_base64", workflow_text)
        self.assertIn("product-onboarding:${PRODUCT}:${GITHUB_RUN_ID}", workflow_text)
        self.assertIn("product-onboarding-result", workflow_text)

    def test_deploy_authz_grants_include_product_onboarding_apply(self) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("product-onboarding.yml", script_text)
        self.assertIn("product_onboarding.apply", script_text)
        self.assertIn("deploy:product-onboarding-grant", script_text)

    def test_deploy_authz_grants_do_not_restore_stale_import_self_deploy_rules(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertNotIn("/v1/authz-policies/github-actions/removals", script_text)
        self.assertNotIn("launchplane-seed-import", script_text)
        self.assertNotIn("stale-merge-train-policy-import-self-deploy", script_text)
        self.assertIn("merge_train.policy_import", script_text)

    def test_deploy_authz_grants_do_not_carry_product_grant_catalog(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("LAUNCHPLANE_AUTHZ_GRANTS_JSON", script_text)
        self.assertNotIn("discord-blue", script_text)
        self.assertNotIn("sellyouroutboard", script_text)
        self.assertNotIn("verireel", script_text)
        self.assertNotIn("odoo-tenant-cm", script_text)
        self.assertNotIn("odoo-tenant-opw", script_text)
        self.assertNotIn("reon-prod", script_text)
        self.assertNotIn("live-target-runtime.yml", script_text)
        self.assertNotIn("product-environment-evidence.yml", script_text)

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
                **OWNER_AUTHZ_ENV,
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

    def test_deploy_authz_grants_include_local_operator_service_read(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("/v1/authz-policies/local-operators/grants", script_text)
        self.assertIn("launchplane_service.read", script_text)
        self.assertIn(
            "deploy:local-operator-launchplane-service-read-grant",
            script_text,
        )
        self.assertIn("local-operator-launchplane-service-read", script_text)

    def test_deploy_authz_grants_include_local_operator_odoo_worker_reconcile(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("launchplane_service.reconcile_odoo_workers", script_text)
        self.assertIn(
            "deploy:local-operator-launchplane-service-reconcile-odoo-workers-grant",
            script_text,
        )
        self.assertIn(
            "local-operator-launchplane-service-reconcile-odoo-workers",
            script_text,
        )

    def test_deploy_authz_grants_include_edge_endpoint_authority(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("edge-endpoint-apply.yml", script_text)
        self.assertIn("edge_endpoint.apply", script_text)
        self.assertIn("edge_endpoint.read", script_text)
        self.assertIn("deploy:edge-endpoint-apply-workflow-grant", script_text)
        self.assertIn("deploy:edge-endpoint-read-workflow-grant", script_text)
        self.assertIn("deploy:local-operator-edge-endpoint-apply-grant", script_text)
        self.assertIn("deploy:local-operator-edge-endpoint-read-grant", script_text)

    def test_deploy_authz_grants_include_private_health_endpoint_authority(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("private_health_endpoint.apply", script_text)
        self.assertIn("private_health_endpoint.read", script_text)
        self.assertIn(
            "deploy:local-operator-private-health-endpoint-apply-grant",
            script_text,
        )
        self.assertIn(
            "deploy:local-operator-private-health-endpoint-read-grant",
            script_text,
        )

    def test_deploy_authz_grants_include_ingress_canary_route_authority(
        self,
    ) -> None:
        script_text = Path("scripts/deploy/ensure-authz-grants.sh").read_text(encoding="utf-8")

        self.assertIn("ingress-route-canary-apply.yml", script_text)
        self.assertIn("ingress_route.apply", script_text)
        self.assertIn("ingress_canary_route.apply", script_text)
        self.assertIn("ingress_canary_route.read", script_text)
        self.assertIn("LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON", script_text)
        self.assertIn("deploy:ingress-route-canary-apply-workflow-${scope_suffix}", script_text)
        self.assertIn("deploy:local-operator-ingress-canary-route-apply-grant", script_text)
        self.assertIn("deploy:local-operator-ingress-canary-route-read-grant", script_text)

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
                **OWNER_AUTHZ_ENV,
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
        private_health_endpoint_grants = [
            grant
            for grant in grants
            if grant["actions"][0].startswith("private_health_endpoint.")
            and "subjects" in grant
            and "token_labels" in grant
        ]
        self.assertEqual(product_config_grants, [])
        self.assertEqual(private_health_endpoint_grants, [])
        self.assertIn(
            "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON is unset or empty; skipping local-operator product_config.plan grant reconciliation.",
            result.stdout,
        )
        self.assertIn(
            "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON is unset or empty; skipping local-operator product_config.apply grant reconciliation.",
            result.stdout,
        )
        self.assertIn(
            "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON is unset or empty; skipping local-operator private_health_endpoint.read grant reconciliation.",
            result.stdout,
        )
        self.assertIn(
            "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON is unset or empty; skipping local-operator private_health_endpoint.apply grant reconciliation.",
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
                **OWNER_AUTHZ_ENV,
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

    def test_deploy_authz_grants_fail_on_malformed_private_health_endpoint_scopes(
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
                **OWNER_AUTHZ_ENV,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON": "not-json",
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

    def test_deploy_authz_grants_reject_private_health_endpoint_wildcard_scopes(
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
                **OWNER_AUTHZ_ENV,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON": json.dumps(
                    [{"product": "*", "context": "*"}]
                ),
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
        self.assertIn(
            "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON must use explicit product/context values",
            result.stderr,
        )

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
                **OWNER_AUTHZ_ENV,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://github.invalid/oidc",
                "GITHUB_REPOSITORY": "cbusillo/launchplane",
                "GITHUB_SHA": "test-sha",
                "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane-service",
                "LAUNCHPLANE_SERVICE_URL": "https://launchplane.example.invalid",
                "LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON": configured_scopes,
                "LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON": configured_scopes,
                "LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON": configured_scopes,
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
        private_health_endpoint_grants = [
            grant
            for grant in grants
            if grant["actions"][0].startswith("private_health_endpoint.")
            and "subjects" in grant
            and "token_labels" in grant
        ]
        scoped_grants = {
            (grant["products"][0], grant["contexts"][0], grant["actions"][0])
            for grant in product_config_grants + private_health_endpoint_grants
        }
        canary_workflow_grants = [
            grant
            for grant in grants
            if grant["actions"] == ["ingress_route.apply"]
            and grant["workflow_refs"][0].endswith(
                "/.github/workflows/ingress-route-canary-apply.yml@refs/heads/main"
            )
        ]
        canary_scoped_grants = {
            (grant["products"][0], grant["contexts"][0]) for grant in canary_workflow_grants
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
                for action in (
                    "product_config.plan",
                    "product_config.apply",
                    "private_health_endpoint.read",
                    "private_health_endpoint.apply",
                )
            },
        )
        self.assertEqual(canary_scoped_grants, expected_scopes)
        for grant in product_config_grants:
            self.assertNotEqual(grant["products"], ["*"])
            self.assertNotEqual(grant["contexts"], ["*"])
            self.assertTrue(
                grant["source_label"].startswith("deploy:local-operator-product-config-")
            )
        for grant in private_health_endpoint_grants:
            self.assertNotEqual(grant["products"], ["*"])
            self.assertNotEqual(grant["contexts"], ["*"])
            self.assertTrue(
                grant["source_label"].startswith("deploy:local-operator-private-health-endpoint-")
            )
        for grant in canary_workflow_grants:
            self.assertNotEqual(grant["products"], ["*"])
            self.assertNotEqual(grant["contexts"], ["*"])
            self.assertTrue(
                grant["source_label"].startswith("deploy:ingress-route-canary-apply-workflow-")
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
        health_check = profile.lanes[0].health_monitoring.checks[0]
        self.assertTrue(health_check.enabled)
        self.assertFalse(health_check.require_runtime_identity)
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

    def test_product_onboarding_manifest_accepts_source_ref_worker_without_http_surface(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "target_name": "cm-repairshopr-sync",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(payload)
            result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-06-12T20:00:00Z",
            )
            profile = store.read_product_profile_record("repairshopr-sync")
            targets = store.list_dokploy_target_records()
            store.close()

        self.assertEqual(result.product, "repairshopr-sync")
        self.assertEqual(profile.image.repository, "")
        self.assertEqual(profile.runtime_port, 0)
        self.assertEqual(profile.health_path, "")
        self.assertEqual(profile.lanes[0].health_url, "")
        self.assertEqual(profile.lanes[0].health_monitoring.checks, ())
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_type, "compose")
        self.assertFalse(targets[0].healthcheck_enabled)
        self.assertEqual(targets[0].healthcheck_path, "")

    def test_product_onboarding_manifest_rejects_image_less_application_target(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "app-123",
                    "target_type": "application",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires compose provider targets"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_compose_without_source(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires source-backed compose"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_compose_without_compose_path(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires compose_path"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_compose_without_branch(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires custom_git_branch"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_http_surface(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "base_url": "https://repairshopr-sync.example.test",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires empty base_url"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_accepts_image_less_explicit_health_url_surface(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_url": "https://repairshopr-sync.example.test/health",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(payload)
            result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-06-12T20:00:00Z",
            )
            profile = store.read_product_profile_record("repairshopr-sync")
            targets = store.list_dokploy_target_records()
            store.close()

        self.assertEqual(result.product, "repairshopr-sync")
        self.assertEqual(profile.image.repository, "")
        self.assertEqual(profile.runtime_port, 0)
        self.assertEqual(profile.health_path, "")
        self.assertEqual(
            profile.lanes[0].health_url,
            "https://repairshopr-sync.example.test/health",
        )
        self.assertEqual(profile.lanes[0].health_monitoring.checks, ())
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target_type, "compose")
        self.assertFalse(targets[0].healthcheck_enabled)

    def test_product_onboarding_manifest_accepts_image_less_health_monitoring(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_url": "https://repairshopr-sync.example.test/health",
                    "health_monitoring": {
                        "checks": [
                            {
                                "name": "public-ingress",
                                "kind": "public_http",
                            }
                        ]
                    },
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(payload)
            apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-06-12T20:00:00Z",
            )
            profile = store.read_product_profile_record("repairshopr-sync")
            store.close()

        self.assertEqual(
            profile.lanes[0].health_url,
            "https://repairshopr-sync.example.test/health",
        )
        self.assertTrue(profile.lanes[0].health_monitoring.checks[0].enabled)

    def test_product_onboarding_manifest_rejects_health_check_alert_issue_url(
        self,
    ) -> None:
        payload = _manifest_payload()
        lanes = payload["lanes"]
        assert isinstance(lanes, list)
        first_lane = lanes[0]
        assert isinstance(first_lane, dict)
        health_monitoring = first_lane["health_monitoring"]
        assert isinstance(health_monitoring, dict)
        checks = health_monitoring["checks"]
        assert isinstance(checks, list)
        first_check = checks[0]
        assert isinstance(first_check, dict)
        first_check["alert_issue_url"] = "https://github.com/example/ops/issues/123"

        with self.assertRaisesRegex(ValueError, "alert_issue_url"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_health_monitoring_without_url(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {
                        "checks": [{"name": "public-ingress", "kind": "public_http"}]
                    },
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires base_url or explicit health_url"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_health_url_without_source_route(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_url": "https://repairshopr-sync.example.test/health",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "staging",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "target must match a stable lane"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_health_url_without_source_backed_target(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_url": "https://repairshopr-sync.example.test/health",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "health surfaces require source-backed compose"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_product_health_surface(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "runtime_port": 8000,
            "health_path": "/health",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires runtime_port=0"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_provider_domains(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": False,
                    "domains": ["repairshopr-sync.example.test"],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires provider targets without domains"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_enabled_target_healthcheck_without_path(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "healthcheck_enabled": True,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "healthcheck requires"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_image_less_target_healthcheck(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "source_type": "git",
                    "custom_git_url": "git@github.com:cbusillo/repairshopr_api.git",
                    "custom_git_branch": "main",
                    "compose_path": "docker/coolify/compose.yml",
                    "healthcheck_enabled": True,
                    "healthcheck_path": "/health",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires disabled provider healthcheck"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_inert_health_monitoring(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "image_repository": "ghcr.io/cbusillo/repairshopr-sync",
            "runtime_port": 3000,
            "health_path": "/health",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {
                        "checks": [{"name": "public-ingress", "kind": "public_http"}]
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires base_url or explicit health_url"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_legacy_public_ingress_monitoring(
        self,
    ) -> None:
        payload = _manifest_payload()
        lanes = payload["lanes"]
        assert isinstance(lanes, list)
        first_lane = lanes[0]
        assert isinstance(first_lane, dict)
        first_lane.pop("health_monitoring")
        first_lane["public_ingress_monitoring"] = {"enabled": True}

        with self.assertRaisesRegex(ValueError, "public_ingress_monitoring"):
            ProductOnboardingManifest.model_validate(payload)

    def test_health_monitoring_migration_preserves_legacy_default_public_check(
        self,
    ) -> None:
        migration = _load_health_monitoring_migration()
        payload = {
            "product": "example-site",
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                },
                {"instance": "worker", "context": "example-site"},
            ],
        }

        migrate_payload = cast(
            Callable[[object], object],
            getattr(migration, "migrate_product_profile_health_monitoring_payload"),
        )
        migrated = cast(dict[str, object], migrate_payload(payload))

        lanes = cast(list[dict[str, object]], migrated["lanes"])
        first_lane_health_monitoring = cast(dict[str, object], lanes[0]["health_monitoring"])
        self.assertEqual(
            first_lane_health_monitoring["checks"],
            [
                {
                    "name": "public-ingress",
                    "kind": "public_http",
                    "enabled": True,
                    "url": "",
                    "require_runtime_identity": False,
                    "provider": "",
                    "provider_check": "",
                }
            ],
        )
        self.assertEqual(lanes[1]["health_monitoring"], {"checks": []})

    def test_health_monitoring_migration_downgrades_first_public_check(self) -> None:
        migration = _load_health_monitoring_migration()
        payload = {
            "product": "example-site",
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                    "health_monitoring": {
                        "checks": [
                            {
                                "name": "private-runtime",
                                "kind": "private_http",
                                "enabled": True,
                                "url": "http://app:3000/healthz",
                            },
                            {
                                "name": "public-ingress",
                                "kind": "public_http",
                                "enabled": True,
                                "require_runtime_identity": True,
                                "alert_issue_url": "https://github.example.test/org/repo/issues/1",
                            },
                        ]
                    },
                },
                {
                    "instance": "worker",
                    "context": "example-site",
                    "health_monitoring": {"checks": []},
                },
            ],
        }

        downgrade_payload = cast(
            Callable[[object], object],
            getattr(migration, "downgrade_product_profile_health_monitoring_payload"),
        )
        downgraded = cast(dict[str, object], downgrade_payload(payload))

        lanes = cast(list[dict[str, object]], downgraded["lanes"])
        self.assertEqual(
            lanes[0]["public_ingress_monitoring"],
            {
                "enabled": True,
                "require_runtime_identity": True,
            },
        )
        self.assertEqual(lanes[1]["public_ingress_monitoring"], {"enabled": False})
        self.assertNotIn("health_monitoring", lanes[0])
        self.assertNotIn("health_monitoring", lanes[1])

    def test_product_profile_rejects_colliding_health_check_names(self) -> None:
        payload = {
            "product": "example-site",
            "display_name": "Example Site",
            "repository": "cbusillo/example-site",
            "driver_id": "generic-web",
            "image_repository": "ghcr.io/cbusillo/example-site",
            "runtime_port": 3000,
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                    "health_monitoring": {
                        "checks": [
                            {"name": "api check", "kind": "public_http"},
                            {
                                "name": "api-check",
                                "kind": "private_http",
                                "private_endpoint_key": "example-site-prod-runtime",
                            },
                        ]
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "health check names must be unique"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_accepts_private_endpoint_health_check(
        self,
    ) -> None:
        payload = {
            "product": "example-site",
            "display_name": "Example Site",
            "repository": "cbusillo/example-site",
            "driver_id": "generic-web",
            "image_repository": "ghcr.io/cbusillo/example-site",
            "runtime_port": 3000,
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                    "health_monitoring": {
                        "checks": [
                            {"name": "public-ingress", "kind": "public_http"},
                            {
                                "name": "private-runtime",
                                "kind": "private_http",
                                "private_endpoint_key": "example-site-prod-runtime",
                            },
                        ]
                    },
                }
            ],
        }

        manifest = ProductOnboardingManifest.model_validate(payload)

        self.assertEqual(
            manifest.lanes[0].health_monitoring.checks[1].private_endpoint_key,
            "example-site-prod-runtime",
        )

    def test_product_profile_rejects_reserved_non_public_health_check_name(self) -> None:
        payload = {
            "product": "example-site",
            "display_name": "Example Site",
            "driver_id": "generic-web",
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                    "health_monitoring": {
                        "checks": [
                            {
                                "name": "public-ingress",
                                "kind": "private_http",
                                "private_endpoint_key": "example-site-prod-runtime",
                            }
                        ]
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "reserved public-ingress name"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_profile_rejects_degenerate_health_check_name(self) -> None:
        payload = {
            "product": "example-site",
            "display_name": "Example Site",
            "driver_id": "generic-web",
            "health_path": "/healthz",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "example-site",
                    "base_url": "https://example.test",
                    "health_monitoring": {"checks": [{"name": "---", "kind": "public_http"}]},
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "alphanumeric"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_base_url_without_health_path(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "image_repository": "ghcr.io/cbusillo/repairshopr-sync",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "base_url": "https://repairshopr-sync.example.test",
                    "health_monitoring": {
                        "checks": [{"name": "public-ingress", "kind": "public_http"}]
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "with base_url requires health_path"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_zero_runtime_port_with_health_path(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "runtime_port": 0,
            "health_path": "/health",
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "runtime_port=0 cannot set health_path"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_runtime_port_without_health_path(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": "generic-web",
            "runtime_port": 8000,
            "lanes": [
                {
                    "instance": "prod",
                    "context": "repairshopr-sync",
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "compose-123",
                    "target_type": "compose",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "runtime_port requires health_path"):
            ProductOnboardingManifest.model_validate(payload)

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
