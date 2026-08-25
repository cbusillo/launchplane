import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
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
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.workflows.product_onboarding import (
    apply_product_onboarding_manifest,
    build_product_profile_record,
)
from tests.support.workflows import load_workflow


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


def _run_runtime_key_safety_generator(
    temporary_directory: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    output_directory = temporary_directory / "runtime-key-safety"
    github_output = temporary_directory / "github-output.txt"
    env = {
        **os.environ,
        "GITHUB_SHA": "test-sha",
        "GITHUB_OUTPUT": str(github_output),
        "LAUNCHPLANE_RUNTIME_KEY_SAFETY_OUTPUT_DIR": str(output_directory),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "scripts/deploy/render-runtime-key-safety-policy.sh"],
        check=False,
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )


def _read_github_outputs(temporary_directory: Path) -> dict[str, str]:
    output_path = temporary_directory / "github-output.txt"
    outputs: dict[str, str] = {}
    if not output_path.exists():
        return outputs
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        outputs[name] = value
    return outputs


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
                    "monitoring_intent": "public",
                    "checks": [
                        {
                            "name": "public-ingress",
                            "kind": "public_http",
                        }
                    ],
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
            "domain_certificate_type": "letsencrypt",
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
    def test_existing_onboarding_preserves_health_monitoring_authority(self) -> None:
        existing_manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
        existing_profile = build_product_profile_record(
            manifest=existing_manifest,
            updated_at="2026-07-27T16:56:00Z",
        )
        replacement_payload = _manifest_payload()
        replacement_lanes = cast(list[dict[str, object]], replacement_payload["lanes"])
        replacement_lanes[0]["health_monitoring"] = {
            "monitoring_intent": "prelaunch",
            "checks": [{"name": "public-ingress", "kind": "public_http"}],
        }
        replacement_manifest = ProductOnboardingManifest.model_validate(replacement_payload)

        replacement_profile = build_product_profile_record(
            manifest=replacement_manifest,
            updated_at="2026-07-27T16:57:00Z",
            existing_profile=existing_profile,
        )

        self.assertEqual(
            replacement_profile.lanes[0].health_monitoring.monitoring_intent,
            "public",
        )

    def test_existing_onboarding_preserves_prelaunch_rebuild_authority(self) -> None:
        existing_manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
        existing_profile = build_product_profile_record(
            manifest=existing_manifest,
            updated_at="2026-07-28T18:00:00Z",
        )
        replacement_payload = _manifest_payload()
        replacement_lanes = cast(list[dict[str, object]], replacement_payload["lanes"])
        replacement_lanes[1]["odoo_prelaunch_rebuild"] = {"enabled": False}
        replacement_manifest = ProductOnboardingManifest.model_validate(replacement_payload)

        replacement_profile = build_product_profile_record(
            manifest=replacement_manifest,
            updated_at="2026-07-28T18:01:00Z",
            existing_profile=existing_profile,
        )

        self.assertEqual(
            replacement_profile.lanes[1].odoo_prelaunch_rebuild,
            existing_profile.lanes[1].odoo_prelaunch_rebuild,
        )

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
        self.assertIn("repository: ${{ steps.publish_inputs.outputs.repository }}", workflow_text)
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
        self.assertIn("repository=result.repository", workflow_text)
        self.assertIn(
            "shared_addons_repository=result.shared_addons_repository",
            workflow_text,
        )
        self.assertIn(
            "product_repository does not match Launchplane product authority.",
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
        self.assertIn(
            '{"schema_version":2,"publish":{"schema_version":2}}',
            workflow_text,
        )
        self.assertNotIn("short_sha=", workflow_text)
        self.assertNotIn("IMAGE_REPOSITORY: ghcr.io/${{ github.repository }}", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-devkit", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-shared-addons", workflow_text)

    def test_product_driver_testing_deploy_requires_explicit_product_scope(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-product-driver-testing-deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("/v1/drivers/odoo/target-replacement-apply", workflow_text)
        self.assertIn(
            "/v1/drivers/odoo/target-replacement/operations/*) ;;",
            workflow_text,
        )
        self.assertIn(
            "Target replacement poll URL is not an Odoo operation path",
            workflow_text,
        )
        self.assertIn(
            "Target replacement poll URL must be a local Launchplane route path.",
            workflow_text,
        )
        self.assertIn(
            "Target replacement poll URL operation ID must be a single path segment.",
            workflow_text,
        )
        self.assertIn("*$'\\n'* | *$'\\r'*)", workflow_text)
        self.assertIn('[[ "$POLL_URL" == *', workflow_text)
        self.assertIn("*'?'*", workflow_text)
        self.assertIn("*'#'*", workflow_text)
        self.assertIn("*'%'*", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn("route-path: ${{ steps.lp.outputs.poll_url }}", workflow_text)
        self.assertIn("poll-result-path: operation.status", workflow_text)
        self.assertIn("poll-result-statuses: pending,running", workflow_text)
        self.assertIn("poll-timeout-ms: ${{ inputs['timeout-ms'] }}", workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn(
            "response-output-file: odoo-testing-target-replacement.json",
            workflow_text,
        )
        self.assertIn("for required in PRODUCT ARTIFACT_ID; do", workflow_text)
        self.assertIn('echo "${required} is required."', workflow_text)
        self.assertNotIn('context_slug="${CONTEXT_NAME//_/-}"', workflow_text)
        self.assertNotIn('product="odoo-tenant-${context_slug}"', workflow_text)
        self.assertIn("product=${{ steps.request.outputs.product }}", workflow_text)
        self.assertIn('"instance": "testing"', workflow_text)
        self.assertIn(
            "replacement.artifact_id=${{ needs.resolve.outputs.artifact_id }}",
            workflow_text,
        )
        self.assertIn("${{ steps.request.outputs.idempotency_key }}", workflow_text)

    def test_product_driver_testing_deploy_requires_explicit_driver(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-product-driver-testing-deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("name: Reusable Product Driver Testing Deploy", workflow_text)
        self.assertIn(
            "driver:\n        description: Product driver id.\n        required: true",
            workflow_text,
        )
        self.assertIn("Unsupported product driver for testing deploy", workflow_text)
        self.assertIn("if: ${{ needs.resolve.outputs.driver == 'odoo' }}", workflow_text)
        self.assertIn("route-path: /v1/drivers/odoo/target-replacement-apply", workflow_text)
        self.assertNotIn("reusable-odoo-testing-deploy.yml", workflow_text)
        self.assertIn("PRODUCT: ${{ needs.resolve.outputs.product }}", workflow_text)
        self.assertIn("ARTIFACT_ID: ${{ needs.resolve.outputs.artifact_id }}", workflow_text)
        self.assertIn(
            "replacement.source_git_ref=${{ needs.resolve.outputs.source_git_ref }}",
            workflow_text,
        )
        self.assertIn("write_output()", workflow_text)
        self.assertIn("printf '%s<<%s\\n'", workflow_text)
        self.assertNotIn('echo "driver=$DRIVER"', workflow_text)
        self.assertNotIn("default: odoo", workflow_text)
        self.assertNotIn('PRODUCT="${GITHUB_REPOSITORY#*/}"', workflow_text)
        self.assertNotIn("permissions:\n  contents: read\n  id-token: write", workflow_text)
        self.assertIn("permissions:\n      contents: read\n      id-token: write", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_reusable_odoo_workflows_use_caller_visible_runner(self) -> None:
        workflow_paths = (
            Path(".github/workflows/reusable-odoo-artifact-publish.yml"),
            Path(".github/workflows/reusable-product-driver-testing-deploy.yml"),
        )

        for workflow_path in workflow_paths:
            with self.subTest(workflow=workflow_path.name):
                workflow_text = workflow_path.read_text(encoding="utf-8")
                self.assertIn("runs-on: ubuntu-latest", workflow_text)
                self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
                self.assertNotIn("runs-on:\n      - self-hosted", workflow_text)

        preview_workflow = Path(".github/workflows/reusable-odoo-preview.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs_on:", preview_workflow)
        self.assertIn("runs-on: ${{ fromJSON(inputs.runs_on) }}", preview_workflow)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", preview_workflow)
        self.assertNotIn("runs-on:\n      - self-hosted", preview_workflow)

    def test_odoo_website_bootstrap_override_requires_explicit_target_inputs(self) -> None:
        dispatcher_text = Path(".github/workflows/odoo-website-bootstrap-override.yml").read_text(
            encoding="utf-8"
        )
        worker_text = Path(
            ".github/workflows/reusable-odoo-website-bootstrap-override.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("product: ${{ inputs.product }}", dispatcher_text)
        self.assertIn("context: ${{ inputs.context }}", dispatcher_text)
        self.assertIn("instance: ${{ inputs.instance }}", dispatcher_text)
        self.assertIn(
            "website_bootstrap_payload: ${{ inputs.website_bootstrap_payload }}", dispatcher_text
        )
        self.assertIn("PRODUCT: ${{ inputs.product }}", worker_text)
        self.assertIn("CONTEXT_NAME: ${{ inputs.context }}", worker_text)
        self.assertIn("INSTANCE: ${{ inputs.instance }}", worker_text)
        self.assertIn("          - prod", dispatcher_text)
        self.assertNotIn("allowed_targets=", worker_text)
        self.assertNotIn("odoo-tenant-opw:opw:testing", worker_text)
        self.assertNotIn("odoo-tenant-opw:opw:prod", worker_text)
        self.assertNotIn("writes only cm/testing", worker_text)

    def test_odoo_website_bootstrap_override_prevalidates_route_paths(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-odoo-website-bootstrap-override.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("website_bootstrap_payload must be a JSON object", workflow_text)
        self.assertIn("homepage_url must be empty or a local Odoo route path", workflow_text)
        self.assertIn("routes[].url values must be empty or local Odoo route paths", workflow_text)
        self.assertIn('startswith("/")', workflow_text)
        self.assertIn('startswith("//") | not', workflow_text)

    def test_odoo_operator_workflows_use_launchplane_request_action(self) -> None:
        workflow_expectations = {
            "odoo-config-parameter-override.yml": (
                "/v1/drivers/odoo/config-parameter-override",
                ".launchplane/odoo-config-parameter-override-payload.json",
                "odoo-config-parameter-override.json",
                'steps.launchplane.outputs.status-code }}" != "202"',
            ),
            "reusable-odoo-target-replacement-plan.yml": (
                "/v1/drivers/odoo/target-replacement-plan",
                ".launchplane/odoo-target-replacement-plan-payload.json",
                "odoo-target-replacement-plan.json",
                'if [ "$PLAN_STATUS_CODE" != "202" ]; then',
            ),
        }

        for workflow_name, (
            route_path,
            payload_file,
            response_file,
            status_check,
        ) in workflow_expectations.items():
            with self.subTest(workflow=workflow_name):
                workflow_text = Path(f".github/workflows/{workflow_name}").read_text(
                    encoding="utf-8"
                )

                self.assertIn("runs-on: ubuntu-latest", workflow_text)
                self.assertIn(
                    "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
                    workflow_text,
                )
                self.assertIn(f"route-path: {route_path}", workflow_text)
                self.assertIn(f"payload-file: {payload_file}", workflow_text)
                self.assertIn(f"response-output-file: {response_file}", workflow_text)
                self.assertIn(
                    "idempotency-key: ${{ steps.request.outputs.idempotency_key }}", workflow_text
                )
                self.assertIn(status_check, workflow_text)
                self.assertIn('!= "accepted"', workflow_text)
                self.assertIn("if: always()", workflow_text)
                self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
                self.assertNotIn("Authorization: Bearer", workflow_text)
                self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)

        website_bootstrap_workflow = Path(
            ".github/workflows/reusable-odoo-website-bootstrap-override.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", website_bootstrap_workflow)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            website_bootstrap_workflow,
        )
        self.assertIn(
            "route-path: /v1/drivers/odoo/website-bootstrap-override",
            website_bootstrap_workflow,
        )
        self.assertIn(
            "payload-file: .launchplane/odoo-website-bootstrap-override-payload.json",
            website_bootstrap_workflow,
        )
        self.assertIn(
            "response-output-file: odoo-website-bootstrap-override.json",
            website_bootstrap_workflow,
        )
        self.assertIn(
            "idempotency-key: ${{ steps.payload.outputs.idempotency_key }}",
            website_bootstrap_workflow,
        )
        self.assertIn('if [ "$STATUS_CODE" != "202" ]; then', website_bootstrap_workflow)
        self.assertIn('!= "accepted"', website_bootstrap_workflow)
        self.assertIn("result.website_bootstrap // false", website_bootstrap_workflow)
        self.assertIn(
            "if: ${{ always() && steps.evidence.outcome == 'success' }}",
            website_bootstrap_workflow,
        )
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", website_bootstrap_workflow)
        self.assertNotIn("Authorization: Bearer", website_bootstrap_workflow)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", website_bootstrap_workflow)

        config_parameter_workflow = Path(
            ".github/workflows/odoo-config-parameter-override.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "group: odoo-config-parameter-override-${{ inputs.product }}-${{ inputs.context }}-${{ inputs.instance }}-${{ inputs.key }}",
            config_parameter_workflow,
        )
        self.assertIn(
            'source_label: "github-actions:odoo-config-parameter-override"',
            config_parameter_workflow,
        )

        stable_bootstrap_workflow = Path(".github/workflows/odoo-stable-bootstrap.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs-on: ubuntu-latest", stable_bootstrap_workflow)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            "route-path: /v1/drivers/odoo/stable-bootstrap",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            "payload-file: .launchplane/odoo-stable-bootstrap-payload.json",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            "response-output-file: odoo-stable-bootstrap-create.json",
            stable_bootstrap_workflow,
        )
        self.assertIn("poll_url=result.poll_url", stable_bootstrap_workflow)
        self.assertIn("operation_id=result.operation_id", stable_bootstrap_workflow)
        self.assertIn(
            'steps.create_bootstrap.outputs.status-code }}" != "202"',
            stable_bootstrap_workflow,
        )
        self.assertIn('!= "accepted"', stable_bootstrap_workflow)
        self.assertIn(
            "for numeric in TIMEOUT_SECONDS HEALTH_TIMEOUT_SECONDS", stable_bootstrap_workflow
        )
        self.assertIn("${numeric} must be a positive integer.", stable_bootstrap_workflow)
        self.assertIn("BOOTSTRAP_POLL_URL", stable_bootstrap_workflow)
        self.assertIn("*://* | //* | *'//'*)", stable_bootstrap_workflow)
        self.assertIn(
            "Odoo bootstrap poll URL must be a local Launchplane route path.",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            "/v1/drivers/odoo/stable-bootstrap/operations/*)",
            stable_bootstrap_workflow,
        )
        self.assertIn("method: GET", stable_bootstrap_workflow)
        self.assertIn(
            "route-path: ${{ steps.create_bootstrap.outputs.poll_url }}",
            stable_bootstrap_workflow,
        )
        self.assertIn("poll-result-path: operation.status", stable_bootstrap_workflow)
        self.assertIn("poll-result-statuses: pending,running", stable_bootstrap_workflow)
        self.assertIn('fail-result-paths: ""', stable_bootstrap_workflow)
        self.assertIn(
            "response-output-file: odoo-stable-bootstrap.json",
            stable_bootstrap_workflow,
        )
        self.assertIn(
            'steps.poll_bootstrap.outputs.status-code }}" != "200"',
            stable_bootstrap_workflow,
        )
        self.assertIn('operation_status" != "pass"', stable_bootstrap_workflow)
        self.assertIn('bootstrap_status" != "pass"', stable_bootstrap_workflow)
        self.assertIn('post_deploy_status" != "pass"', stable_bootstrap_workflow)
        self.assertIn("odoo-stable-bootstrap-create.json", stable_bootstrap_workflow)
        self.assertIn("if: always()", stable_bootstrap_workflow)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", stable_bootstrap_workflow)
        self.assertNotIn("Authorization: Bearer", stable_bootstrap_workflow)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", stable_bootstrap_workflow)
        self.assertNotIn("LAUNCHPLANE_SERVICE_URL", stable_bootstrap_workflow)
        self.assertNotIn("curl ", stable_bootstrap_workflow)

        target_apply_workflow = Path(
            ".github/workflows/reusable-odoo-target-replacement-apply.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", target_apply_workflow)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            target_apply_workflow,
        )
        self.assertIn(
            "route-path: /v1/drivers/odoo/target-replacement-apply",
            target_apply_workflow,
        )
        self.assertIn(
            "payload-file: .launchplane/odoo-target-replacement-apply-payload.json",
            target_apply_workflow,
        )
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            target_apply_workflow,
        )
        self.assertIn(
            "response-output-file: odoo-target-replacement-apply-create.json",
            target_apply_workflow,
        )
        self.assertIn("poll_url=result.poll_url", target_apply_workflow)
        self.assertIn("operation_id=result.operation_id", target_apply_workflow)
        self.assertIn(
            "CREATE_STATUS_CODE: ${{ steps.create_replacement.outputs.status-code }}",
            target_apply_workflow,
        )
        self.assertIn(
            'if [ "$CREATE_STATUS_CODE" != "202" ]; then',
            target_apply_workflow,
        )
        self.assertIn('!= "accepted"', target_apply_workflow)
        self.assertIn(
            "for numeric in TIMEOUT_SECONDS HEALTH_TIMEOUT_SECONDS", target_apply_workflow
        )
        self.assertIn("${numeric} must be a positive integer.", target_apply_workflow)
        self.assertIn("REPLACEMENT_POLL_URL", target_apply_workflow)
        self.assertIn("CREATE_STATUS_CODE", target_apply_workflow)
        self.assertIn("POLL_STATUS_CODE", target_apply_workflow)
        self.assertIn('case "$DATA_SOURCE_MODE" in', target_apply_workflow)
        self.assertIn("existing | empty | upstream_restore)", target_apply_workflow)
        self.assertIn(
            "*$'\\n'* | *$'\\r'* | *://* | //* | *'//'* | *'?'* | *'#'* | *'%'*)",
            target_apply_workflow,
        )
        self.assertIn(
            "Odoo target replacement poll URL must be a local route path.",
            target_apply_workflow,
        )
        self.assertIn(
            "/v1/drivers/odoo/target-replacement/operations/*)",
            target_apply_workflow,
        )
        self.assertIn("method: GET", target_apply_workflow)
        self.assertIn(
            "route-path: ${{ steps.create_replacement.outputs.poll_url }}",
            target_apply_workflow,
        )
        self.assertIn("poll-result-path: operation.status", target_apply_workflow)
        self.assertIn("poll-result-statuses: pending,running", target_apply_workflow)
        self.assertIn('fail-result-paths: ""', target_apply_workflow)
        self.assertIn(
            "response-output-file: odoo-target-replacement-apply.json",
            target_apply_workflow,
        )
        self.assertIn(
            "POLL_STATUS_CODE: ${{ steps.poll_replacement.outputs.status-code }}",
            target_apply_workflow,
        )
        self.assertIn(
            'if [ "$POLL_STATUS_CODE" != "200" ]; then',
            target_apply_workflow,
        )
        self.assertIn('operation_status" != "pass"', target_apply_workflow)
        self.assertIn('deploy_status" != "pass"', target_apply_workflow)
        self.assertIn('post_deploy_status" != "pass"', target_apply_workflow)
        self.assertIn("odoo-target-replacement-apply-create.json", target_apply_workflow)
        self.assertIn("if: always()", target_apply_workflow)
        target_apply_wrapper = Path(
            ".github/workflows/odoo-target-replacement-apply.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "group: >-\n    odoo-target-replacement-apply-${{ inputs.product }}-${{ inputs.instance }}",
            target_apply_wrapper,
        )
        self.assertIn("cancel-in-progress: false", target_apply_wrapper)
        self.assertNotIn(
            '${{ steps.create_replacement.outputs.status-code }}" !=', target_apply_workflow
        )
        self.assertNotIn(
            '${{ steps.poll_replacement.outputs.status-code }}" !=', target_apply_workflow
        )
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", target_apply_workflow)
        self.assertNotIn("Authorization: Bearer", target_apply_workflow)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", target_apply_workflow)
        self.assertNotIn("LAUNCHPLANE_SERVICE_URL", target_apply_workflow)
        self.assertNotIn("curl ", target_apply_workflow)

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

        provider_target_workflow = Path(
            ".github/workflows/provider-target-operations.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", provider_target_workflow)
        self.assertIn(
            "route_matrix: ${{ steps.routes.outputs.route_matrix }}",
            provider_target_workflow,
        )
        self.assertIn(
            "route: ${{ fromJson(needs.resolve.outputs.route_matrix) }}",
            provider_target_workflow,
        )
        self.assertIn("inputs.provider_id", provider_target_workflow)
        self.assertIn("github.run_id", provider_target_workflow)
        self.assertIn(
            "routes_json must be a non-empty array of context/instance objects.",
            provider_target_workflow,
        )
        self.assertIn("fail-fast: false", provider_target_workflow)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            provider_target_workflow,
        )
        self.assertIn(
            "audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",
            provider_target_workflow,
        )
        self.assertIn("route-path: /v1/provider-targets/operations", provider_target_workflow)
        self.assertIn(
            "payload-file: ${{ steps.request.outputs.payload_file }}",
            provider_target_workflow,
        )
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            provider_target_workflow,
        )
        self.assertIn("fail-result-paths: result.operation_status", provider_target_workflow)
        self.assertIn(
            "response-output-file: ${{ steps.request.outputs.response_file }}",
            provider_target_workflow,
        )
        self.assertIn(
            "STATUS_CODE: ${{ steps.provider_target_request.outputs.status-code }}",
            provider_target_workflow,
        )
        self.assertIn('if [ "$STATUS_CODE" != "202" ]; then', provider_target_workflow)
        self.assertIn('if [ "$operation_status" != "ok" ]; then', provider_target_workflow)
        self.assertIn("if-no-files-found: warn", provider_target_workflow)
        self.assertNotIn("actions/checkout", provider_target_workflow)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", provider_target_workflow)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", provider_target_workflow)
        self.assertNotIn("Authorization: Bearer", provider_target_workflow)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", provider_target_workflow)
        self.assertNotIn("curl ", provider_target_workflow)

    def test_product_environment_evidence_uses_launchplane_request(self) -> None:
        workflow_text = Path(".github/workflows/product-environment-evidence.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn(
            "route_matrix: ${{ steps.routes.outputs.route_matrix }}",
            workflow_text,
        )
        self.assertIn(
            "route: ${{ fromJson(needs.resolve.outputs.route_matrix) }}",
            workflow_text,
        )
        self.assertIn("product:.value.product", workflow_text)
        self.assertIn("environment:.value.environment", workflow_text)
        self.assertIn("fail-fast: false", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertEqual(
            workflow_text.count("uses: cbusillo/launchplane/.github/actions/launchplane-request@"),
            2,
        )
        self.assertIn("launchplane-url: ${{ env.LAUNCHPLANE_URL }}", workflow_text)
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn("route-path: ${{ steps.request.outputs.environment_route }}", workflow_text)
        self.assertIn("route-path: ${{ steps.request.outputs.config_status_route }}", workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn('log-response-body: "false"', workflow_text)
        self.assertIn(
            "response-output-file: ${{ steps.request.outputs.environment_response_file }}",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: ${{ steps.request.outputs.config_status_response_file }}",
            workflow_text,
        )
        self.assertIn('quote(product, safe="")', workflow_text)
        self.assertIn('quote(environment, safe="")', workflow_text)
        self.assertIn(
            "STATUS_CODE: ${{ steps.environment_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn(
            "STATUS_CODE: ${{ steps.config_status_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn(
            'all($config_status.runtime_settings[]; .status == "configured")', workflow_text
        )
        self.assertIn(
            'all($config_status.managed_secrets[]; .status == "configured")', workflow_text
        )
        self.assertIn('$config_status.trust_state == "recorded"', workflow_text)
        self.assertIn('if [ "$status" != "ok" ]; then', workflow_text)
        self.assertIn("Product environment config status was not ok", workflow_text)
        self.assertIn("Write product environment step summary", workflow_text)
        self.assertIn("product-environment-evidence-${TARGET_SET}-${ROUTE_INDEX}", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
        self.assertNotIn("service_audience", workflow_text)
        self.assertNotIn("oidc_token", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_dokploy_target_setup_workflow_supports_compose_domain_reconcile(self) -> None:
        workflow_text = Path(".github/workflows/dokploy-target-setup.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("route-path: /v1/dokploy-targets/setup", workflow_text)
        self.assertIn("payload-file: dokploy-target-setup-payload.json", workflow_text)
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            workflow_text,
        )
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn("response-output-file: dokploy-target-setup.json", workflow_text)
        self.assertIn(
            "SETUP_STATUS_CODE: ${{ steps.target_setup_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn('if [ "$SETUP_STATUS_CODE" != "202" ]; then', workflow_text)
        self.assertIn("- reconcile-compose-domain", workflow_text)
        self.assertIn("DOMAIN: ${{ inputs.domain }}", workflow_text)
        self.assertIn("RUNTIME_PORT: ${{ inputs.runtime_port }}", workflow_text)
        self.assertIn("Validate reconcile compose domain inputs", workflow_text)
        self.assertIn("domain is required for compose domain reconcile/prune", workflow_text)
        self.assertIn("runtime_port is required for reconcile-compose-domain", workflow_text)
        self.assertIn("deploy_timeout_seconds must be a positive integer.", workflow_text)
        self.assertIn("expected_current_provider_target_json:", workflow_text)
        self.assertIn("EXPECTED_CURRENT_PROVIDER_TARGET_JSON", workflow_text)
        self.assertIn("expected_current_provider_target", workflow_text)
        self.assertIn("APPLY DOKPLOY TARGET SETUP", workflow_text)
        self.assertIn("dokploy-target-setup-payload.json", workflow_text)
        self.assertIn("dokploy-target-setup:${OPERATION}:", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("actions/checkout", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_live_target_runtime_workflow_uses_launchplane_request(self) -> None:
        workflow_text = Path(".github/workflows/live-target-runtime.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn("Deploy options require mode=apply.", workflow_text)
        self.assertIn("live-target-runtime-request.json", workflow_text)
        self.assertIn("live-target-runtime-response.json", workflow_text)
        self.assertIn("payload_file=${payload_file}", workflow_text)
        self.assertIn("response_file=${response_file}", workflow_text)
        self.assertIn("idempotency_key=${idempotency_key}", workflow_text)
        self.assertIn(
            "live-target-runtime:${MODE}:${CONTEXT}:${INSTANCE}:${GITHUB_RUN_ID}:${GITHUB_RUN_ATTEMPT}",
            workflow_text,
        )
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("launchplane-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}", workflow_text)
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("route-path: /v1/live-target-runtime/apply", workflow_text)
        self.assertIn("payload-file: ${{ steps.request.outputs.payload_file }}", workflow_text)
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            workflow_text,
        )
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn(
            "response-output-file: ${{ steps.request.outputs.response_file }}",
            workflow_text,
        )
        self.assertIn('log-response-body: "false"', workflow_text)
        self.assertIn(
            "STATUS_CODE: ${{ steps.live_target_runtime_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn("Summarize live target runtime sync", workflow_text)
        self.assertIn("$result.runtime_key_safety.status", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("service_audience", workflow_text)
        self.assertNotIn("oidc_token", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_reusable_odoo_workflows_accept_configured_service_identity(self) -> None:
        workflow_paths = (
            Path(".github/workflows/reusable-odoo-artifact-publish.yml"),
            Path(".github/workflows/reusable-product-driver-testing-deploy.yml"),
            Path(".github/workflows/reusable-odoo-preview.yml"),
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

    def test_reusable_odoo_preview_owns_preview_request_chain(self) -> None:
        workflow_path = Path(".github/workflows/reusable-odoo-preview.yml")
        workflow_text = workflow_path.read_text(encoding="utf-8")

        self.assertIn("name: Reusable Odoo Preview", workflow_text)
        self.assertIn("workflow_call:", workflow_text)
        self.assertIn("product:", workflow_text)
        self.assertIn("context:", workflow_text)
        self.assertIn("operation:", workflow_text)
        self.assertIn("pr_number:", workflow_text)
        self.assertIn("runs_on:", workflow_text)
        self.assertIn("runs-on: ${{ fromJSON(inputs.runs_on) }}", workflow_text)
        self.assertIn("operation must be refresh or destroy.", workflow_text)
        self.assertIn("product must be a single slug value.", workflow_text)
        self.assertIn("context must be a single slug value.", workflow_text)
        self.assertIn("pr_url must be a single-line value.", workflow_text)
        self.assertIn("source_git_ref is required for Odoo preview.", workflow_text)
        self.assertIn("needs.validate.outputs.operation == 'refresh'", workflow_text)
        self.assertIn("needs.validate.outputs.operation == 'destroy'", workflow_text)
        self.assertIn("needs.validate.outputs.pr_url", workflow_text)
        self.assertIn("needs.validate.outputs.source_git_ref", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/setup-odoo-preview-request-client@",
            workflow_text,
        )
        self.assertEqual(workflow_text.count("          request-kind: artifact-publish-inputs"), 1)
        self.assertEqual(workflow_text.count("          request-kind: preview-apply-inputs"), 2)
        self.assertEqual(workflow_text.count("          request-kind: preview-apply\n"), 2)
        self.assertIn("steps.publish_inputs_request.outputs.route-path", workflow_text)
        self.assertIn("steps.dry_run_request.outputs.payload-json-files", workflow_text)
        self.assertIn("idempotency-key: ${{ steps.dry_run.outputs.plan_id }}", workflow_text)
        self.assertIn("image_repository=result.image_repository", workflow_text)
        self.assertIn("preview_slug=result.preview_slug", workflow_text)
        self.assertIn("refresh_image_reference", workflow_text)
        self.assertIn("cleanup_failure_summary", workflow_text)
        self.assertIn("preview-refresh-feedback:", workflow_text)
        self.assertIn("preview-destroy-feedback:", workflow_text)
        self.assertIn(
            "uses: ./.github/workflows/reusable-preview-feedback-status.yml",
            workflow_text,
        )
        self.assertIn("source_access_probe_repository", workflow_text)
        self.assertIn('default: ""', workflow_text)
        self.assertIn(
            "No source access probe repository configured; skipping probe.", workflow_text
        )
        self.assertIn("tenant_repository must match the caller repository.", workflow_text)
        self.assertIn("tenant_path must be a single relative directory name.", workflow_text)
        self.assertIn("source_git_ref must be a single-line ref.", workflow_text)
        self.assertIn("source_git_ref contains unsupported characters.", workflow_text)
        self.assertIn("odoo-devkit", workflow_text)
        self.assertIn("odoo-shared-addons", workflow_text)
        self.assertIn("odoo-preview-publish-inputs.json", workflow_text)
        self.assertIn("odoo-preview-artifact.json", workflow_text)
        self.assertIn("odoo-preview-dry-run.json", workflow_text)
        self.assertIn("odoo-preview-destroy-dry-run.json", workflow_text)
        self.assertIn("trap 'rm -f", workflow_text)
        self.assertIn(
            "(needs.preview-refresh.result == 'success' || needs.preview-refresh.result == 'failure')",
            workflow_text,
        )
        self.assertIn(
            "(needs.preview-destroy.result == 'success' || needs.preview-destroy.result == 'failure')",
            workflow_text,
        )
        self.assertRegex(
            workflow_text,
            r"uses: actions/checkout@(?:v\d+(?:\.\d+){0,2}|[0-9a-f]{40})(?:\s|$)",
        )
        self.assertIn("source_git_ref is required for Odoo preview destroy.", workflow_text)
        self.assertIn("timeout-ms: ${{ inputs['timeout-ms'] }}", workflow_text)
        self.assertIn("failure_summary=\"${failure_summary//$'\\r'/ }\"", workflow_text)
        self.assertIn(
            "cleanup_failure_summary=\"${cleanup_failure_summary//$'\\n'/ }\"",
            workflow_text,
        )
        self.assertNotIn("node --input-type=module", workflow_text)
        self.assertNotIn("route-path: /v1/drivers/odoo/preview-apply", workflow_text)
        self.assertNotIn("route-path: /v1/drivers/odoo/artifact-publish-inputs", workflow_text)
        self.assertNotIn("odoo-preview-apply:", workflow_text)
        self.assertNotIn("odoo-preview-apply-inputs:", workflow_text)
        self.assertNotIn("odoo-artifact-publish-inputs:", workflow_text)
        self.assertIn("plan-id: ${{ steps.dry_run.outputs.plan_id }}", workflow_text)
        self.assertIn("plan_id=result.plan_provenance.plan_id", workflow_text)
        self.assertEqual(
            workflow_text.count("idempotency-key: ${{ steps.dry_run.outputs.plan_id }}"),
            2,
        )
        self.assertNotIn("disable_odoo_online", workflow_text)
        self.assertNotIn("devkit_repository:", workflow_text)
        self.assertNotIn("shared_addons_repository:", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-devkit", workflow_text)
        self.assertNotIn("repository: cbusillo/odoo-shared-addons", workflow_text)
        self.assertNotIn("preview-slug: pr-", workflow_text)
        self.assertNotIn("${{ runner.temp }}/${{ inputs.context }}", workflow_text)
        self.assertNotIn("${CONTEXT_NAME}-isolated-preview", workflow_text)
        self.assertNotIn(
            "INPUT_SOURCE_GIT_REF: ${{ inputs.source_git_ref || github.sha }}", workflow_text
        )

    def test_reusable_odoo_preview_inherits_refresh_permissions_from_caller(self) -> None:
        workflow_text = Path(".github/workflows/reusable-odoo-preview.yml").read_text(
            encoding="utf-8"
        )
        refresh_job = workflow_text.split("\n  preview-refresh:\n", 1)[1].split(
            "\n  preview-refresh-feedback:\n", 1
        )[0]

        self.assertNotIn("\n    permissions:\n", refresh_job)
        self.assertNotIn("packages: write", refresh_job)

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
        self.assertRegex(
            workflow_text,
            r"uses: actions/checkout@(?:v\d+(?:\.\d+){0,2}|[0-9a-f]{40})(?:\s|$)",
        )
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("route-path: /v1/drivers/odoo/artifact-publish-inputs", workflow_text)
        self.assertIn("Render authenticated Odoo route probe payloads", workflow_text)
        self.assertIn("Probe Odoo preview apply inputs route", workflow_text)
        self.assertIn("Probe Odoo preview apply route", workflow_text)
        self.assertIn("Probe preview PR feedback route", workflow_text)
        self.assertEqual(workflow_text.count("continue-on-error: true"), 3)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("Classify authenticated Odoo route responses", workflow_text)
        self.assertIn("/v1/drivers/odoo/preview-apply-inputs", workflow_text)
        self.assertIn("/v1/drivers/odoo/preview-apply", workflow_text)
        self.assertIn("/v1/previews/pr-feedback", workflow_text)
        self.assertIn("expected-status: 202,403,409,503", workflow_text)
        self.assertIn('expected-status: "202"', workflow_text)
        self.assertEqual(workflow_text.count('timeout-ms: "30000"'), 3)
        self.assertIn(
            "response-output-file: ${{ steps.route_probes.outputs.preview_apply_response }}",
            workflow_text,
        )
        self.assertIn(
            "PREVIEW_APPLY_STATUS: ${{ steps.preview_apply_probe.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn("dry_run: true", workflow_text)
        self.assertIn("'.result.preview_pr_feedback // \"\"'", workflow_text)
        self.assertIn("authorized", workflow_text)
        self.assertIn("accepted a non-dry-run feedback probe", workflow_text)
        self.assertIn("preserved structured evidence", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn('--data-urlencode "audience=', workflow_text)
        self.assertIn(
            "Artifact publish inputs did not include the product repository", workflow_text
        )
        self.assertIn("PRODUCT_REPOSITORY", workflow_text)
        self.assertIn("source_repository=result.repository", workflow_text)
        self.assertIn('operation: "destroy"', workflow_text)
        self.assertIn('code: "runtime_plan_not_ready"', workflow_text)
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
        wrapper_text = Path(".github/workflows/ingress-route-dry-run.yml").read_text(
            encoding="utf-8"
        )
        workflow_text = Path(".github/workflows/reusable-ingress-route-dry-run.yml").read_text(
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
        inputs_section = wrapper_text.split("permissions:", maxsplit=1)[0]
        self.assertEqual(inputs_section.count("        description:"), 12)
        self.assertIn("      instance:", inputs_section)
        self.assertIn(
            "reusable-ingress-route-dry-run.yml@b649f41982c478189aabb7c9e5a5e8649279b01b",
            wrapper_text,
        )
        self.assertIn("edge_endpoint_key:", inputs_section)
        self.assertIn('default: ""', inputs_section)
        self.assertNotIn("identity_access_provider:", inputs_section)
        self.assertNotIn("identity_access_send_basic_auth:", inputs_section)
        self.assertRegex(
            workflow_text,
            r"uses: actions/checkout@(?:v\d+(?:\.\d+){0,2}|[0-9a-f]{40})(?:\s|$)",
        )
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
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
            self.assertIn(f"          - {forward_scheme}", wrapper_text)

    def test_ingress_route_audit_read_workflow_is_plan_scoped_get(self) -> None:
        workflow_text = Path(".github/workflows/ingress-route-audit-read.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn("route-path: ${{ steps.route.outputs.route_path }}", workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn("response-output-file: ingress-route-audit-read-raw.json", workflow_text)
        self.assertIn('log-response-body: "false"', workflow_text)
        self.assertIn(
            "STATUS_CODE: ${{ steps.audit_read_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn("Launchplane ingress route audit read did not produce", workflow_text)
        self.assertIn("if [ \"$STATUS_CODE\" != '200' ]; then", workflow_text)
        self.assertIn("non-JSON error response", workflow_text)
        self.assertNotIn('cat "$raw_response"', workflow_text)
        self.assertIn('echo "route_path=${read_url}"', workflow_text)
        self.assertIn("/v1/ingress/route-audits/records", workflow_text)
        self.assertIn("product", workflow_text)
        self.assertIn("context", workflow_text)
        self.assertIn("record_id", workflow_text)
        self.assertIn("limit must be between 1 and 100", workflow_text)
        self.assertIn('raw_response="ingress-route-audit-read-raw.json"', workflow_text)
        self.assertIn("redacted", workflow_text)
        self.assertIn("operation_count", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("path: ingress-route-audit-read.json", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
        self.assertNotIn("curl ", workflow_text)
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
        wrapper_text = Path(".github/workflows/ingress-route-apply.yml").read_text(encoding="utf-8")
        workflow_text = Path(".github/workflows/reusable-ingress-route-apply.yml").read_text(
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
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("route-path: /v1/drivers/ingress/route-apply", workflow_text)
        self.assertIn("PRODUCT: ${{ inputs.product }}", workflow_text)
        self.assertIn("CONTEXT: ${{ inputs.context }}", workflow_text)
        self.assertIn('echo "- Product: $PRODUCT"', workflow_text)
        self.assertIn('echo "- Context: $CONTEXT"', workflow_text)
        self.assertNotIn('echo "- Product: ${{ inputs.product }}"', workflow_text)
        self.assertNotIn('echo "- Context: ${{ inputs.context }}"', workflow_text)
        inputs_section = wrapper_text.split("permissions:", maxsplit=1)[0]
        self.assertEqual(inputs_section.count("        description:"), 7)
        self.assertIn("      instance:", inputs_section)
        self.assertIn(
            "reusable-ingress-route-apply.yml@b649f41982c478189aabb7c9e5a5e8649279b01b",
            wrapper_text,
        )

    def test_preview_lifecycle_uses_launchplane_request(self) -> None:
        workflow_text = Path(".github/workflows/preview-lifecycle.yml").read_text(encoding="utf-8")

        self.assertIn("- self-hosted", workflow_text)
        self.assertIn("- ${{ vars.LAUNCHPLANE_RUNNER_LABEL }}", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("launchplane-url: ${{ env.LAUNCHPLANE_URL }}", workflow_text)
        self.assertIn("audience: ${{ env.LAUNCHPLANE_AUDIENCE }}", workflow_text)
        self.assertIn("route-path: /v1/previews/lifecycle-sweep", workflow_text)
        self.assertIn("payload-file: ${{ steps.request.outputs.request_file }}", workflow_text)
        self.assertIn(
            "idempotency-key: ${{ steps.request.outputs.idempotency_key }}",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: launchplane-preview-lifecycle-sweep-response.json",
            workflow_text,
        )
        self.assertIn("request_file=", workflow_text)
        self.assertIn("launchplane-preview-lifecycle-sweep.json", workflow_text)
        self.assertIn("launchplane-preview-lifecycle-sweep-response.json", workflow_text)
        self.assertIn("steps.request.outputs.apply_json", workflow_text)
        self.assertNotIn("post_launchplane_json", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_github_metadata_prefers_repository_merge_method(self) -> None:
        metadata = json.loads(Path(".github/github.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["pullRequests"]["preferredMergeMethod"], "merge")
        self.assertEqual(metadata["pullRequests"]["allowedMergeMethods"], ["merge"])
        self.assertIn("Ingress Route Apply", metadata["importantWorkflows"])
        self.assertNotIn("healthUrls", metadata)

    def test_product_driver_testing_deploy_exposes_result_outputs(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-product-driver-testing-deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "replacement.source_git_ref=${{ needs.resolve.outputs.source_git_ref }}",
            workflow_text,
        )
        self.assertIn("outputs:", workflow_text)
        self.assertIn(
            "value: ${{ jobs.odoo_testing_deploy.outputs.deployment_record_id }}",
            workflow_text,
        )
        self.assertIn(
            "deployment_record_id: ${{ steps.poll.outputs.deployment_record_id }}", workflow_text
        )
        self.assertIn(
            "POLL_STATUS_CODE: ${{ steps.poll_result.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn('if [ "$POLL_STATUS_CODE" != "200" ]; then', workflow_text)
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
        self.assertIn('if $compose_external_network != "" then', workflow_text)
        self.assertIn(
            "{LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK: $compose_external_network}",
            workflow_text,
        )
        self.assertNotIn("omit_compose_external_network_env", workflow_text)

    def test_deploy_launchplane_requires_manager_preview_webhook_secret(self) -> None:
        workflow_path = Path(".github/workflows/deploy-launchplane.yml")
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = load_workflow(workflow_path)
        validate_step = workflow.step_named("deploy", "Validate deploy configuration")
        previous_runtime_step = workflow.step_named("deploy", "Read previous Launchplane runtime")
        render_step = workflow.step_named("deploy", "Render Launchplane self deploy request")

        self.assertIsNotNone(validate_step)
        self.assertIsNotNone(previous_runtime_step)
        self.assertIsNotNone(render_step)
        assert validate_step is not None
        assert previous_runtime_step is not None
        assert render_step is not None
        self.assertLess(validate_step.index, previous_runtime_step.index)
        self.assertIn(
            "Missing LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET secret.",
            validate_step.run,
        )
        self.assertNotIn(
            "Missing LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET secret.",
            render_step.run,
        )

        self.assertIn(
            "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET: "
            "${{ secrets.LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET }}",
            workflow_text,
        )
        self.assertIn(
            "--arg manager_preview_webhook_secret "
            '"${LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET:-}"',
            workflow_text,
        )
        self.assertIn(
            "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET: $manager_preview_webhook_secret",
            workflow_text,
        )

    def test_deploy_launchplane_manages_key_ring_only_by_explicit_operation(self) -> None:
        workflow_path = Path(".github/workflows/deploy-launchplane.yml")
        workflow = load_workflow(workflow_path)
        trigger = workflow.data["on"]
        assert isinstance(trigger, dict)
        workflow_dispatch = trigger["workflow_dispatch"]
        assert isinstance(workflow_dispatch, dict)
        inputs = workflow_dispatch["inputs"]
        assert isinstance(inputs, dict)
        bootstrap_operation = inputs["bootstrap_secret_operation"]
        assert isinstance(bootstrap_operation, dict)
        self.assertEqual(bootstrap_operation["default"], "preserve")
        self.assertEqual(bootstrap_operation["options"], ["preserve", "install", "remove"])
        idempotency_key = inputs["self_deploy_idempotency_key"]
        assert isinstance(idempotency_key, dict)
        self.assertEqual(idempotency_key["required"], False)

        workflow_text = workflow_path.read_text(encoding="utf-8")
        self.assertIn(
            "LAUNCHPLANE_SECRET_KEYS_JSON: ${{ secrets.LAUNCHPLANE_SECRET_KEYS_JSON }}",
            workflow_text,
        )
        deploy_job = workflow.job("deploy")
        deploy_environment = deploy_job["env"]
        assert isinstance(deploy_environment, dict)
        self.assertNotIn("LAUNCHPLANE_SECRET_KEYS_JSON", deploy_environment)
        render_step = workflow.step_named("deploy", "Render Launchplane self deploy request")
        self.assertIsNotNone(render_step)
        assert render_step is not None
        render_environment = render_step.data["env"]
        assert isinstance(render_environment, dict)
        self.assertEqual(
            render_environment["LAUNCHPLANE_SECRET_KEYS_JSON"],
            "${{ secrets.LAUNCHPLANE_SECRET_KEYS_JSON }}",
        )
        self.assertIn(
            "bootstrap_secret_operation must be preserve, install, or remove.",
            workflow_text,
        )
        self.assertIn(
            "self_deploy_idempotency_key must be an 8-200 character safe token.",
            workflow_text,
        )
        self.assertIn(
            "Automatic deploys must preserve bootstrap secret configuration.",
            workflow_text,
        )
        self.assertIn(
            "bootstrap_secret_operation=install requires LAUNCHPLANE_SECRET_KEYS_JSON.",
            workflow_text,
        )
        self.assertIn(
            "Bootstrap secret operations must retain the current deployed image.",
            workflow_text,
        )
        self.assertIn('echo "::add-mask::$secret_keys_json"', workflow_text)
        self.assertIn(
            "{LAUNCHPLANE_SECRET_KEYS_JSON: $secret_keys_json}",
            workflow_text,
        )
        self.assertIn('["LAUNCHPLANE_SECRET_KEYS_JSON"]', workflow_text)
        self.assertIn(
            "steps.prep.outputs.self_deploy_idempotency_key ||",
            workflow_text,
        )
        cleanup_step = workflow.step_named(
            "deploy", "Remove Launchplane self deploy request material"
        )
        self.assertIsNotNone(cleanup_step)
        assert cleanup_step is not None
        self.assertEqual(cleanup_step.data["if"], "always()")
        self.assertNotIn("steps.self_deploy.outputs.payload_file", cleanup_step.run)
        self.assertIn(
            '"$RUNNER_TEMP/launchplane-self-deploy-payload.json"',
            cleanup_step.run,
        )

    def test_deploy_launchplane_validates_manual_bootstrap_inputs(self) -> None:
        workflow = load_workflow(".github/workflows/deploy-launchplane.yml")
        prep_step = workflow.step_named("deploy", "Resolve deploy inputs")
        self.assertIsNotNone(prep_step)
        assert prep_step is not None
        image_reference = "ghcr.io/cbusillo/launchplane@sha256:" + ("a" * 64)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)

            def prepare(
                *,
                event_name: str,
                operation: str,
                idempotency_key: str,
            ) -> subprocess.CompletedProcess[str]:
                output_file = temporary_directory / "github-output.txt"
                output_file.unlink(missing_ok=True)
                return subprocess.run(
                    ["bash", "-ceu", prep_step.run],
                    check=False,
                    capture_output=True,
                    env={
                        **os.environ,
                        "DISPATCH_BOOTSTRAP_SECRET_OPERATION": operation,
                        "DISPATCH_IMAGE_REFERENCE": image_reference,
                        "DISPATCH_SELF_DEPLOY_IDEMPOTENCY_KEY": idempotency_key,
                        "EVENT_NAME": event_name,
                        "GITHUB_OUTPUT": str(output_file),
                        "GITHUB_REPOSITORY": "cbusillo/launchplane",
                        "LAUNCHPLANE_IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                        "OMIT_EVERY_CODE_ENV": "false",
                        "OMIT_NPMPLUS_ENV": "false",
                        "OMIT_OWNER_AGENT_ENV": "false",
                        "OMIT_TERMINAL_AGENT_ENV": "false",
                        "WORKFLOW_RUN_HEAD_SHA": "b" * 40,
                        "WORKFLOW_SHA": "c" * 40,
                    },
                    text=True,
                )

            valid_key = "launchplane-self-deploy:issue-2204:root-2026-08-25:install:v1"
            valid = prepare(
                event_name="workflow_dispatch",
                operation="install",
                idempotency_key=valid_key,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            outputs = _read_github_outputs(temporary_directory)
            self.assertEqual(outputs["bootstrap_secret_operation"], "install")
            self.assertEqual(outputs["self_deploy_idempotency_key"], valid_key)

            unsafe = prepare(
                event_name="workflow_dispatch",
                operation="install",
                idempotency_key="unsafe value",
            )
            self.assertNotEqual(unsafe.returncode, 0)
            self.assertIn("8-200 character safe token", unsafe.stderr)

            automatic = prepare(
                event_name="workflow_run",
                operation="remove",
                idempotency_key="",
            )
            self.assertNotEqual(automatic.returncode, 0)
            self.assertIn("Automatic deploys must preserve", automatic.stderr)

    def test_deploy_launchplane_renders_key_ring_install_preserve_and_remove(self) -> None:
        workflow = load_workflow(".github/workflows/deploy-launchplane.yml")
        render_step = workflow.step_named("deploy", "Render Launchplane self deploy request")
        self.assertIsNotNone(render_step)
        assert render_step is not None
        image_reference = "ghcr.io/cbusillo/launchplane@sha256:" + ("a" * 64)
        key_ring = {
            "active_key_id": "root-test",
            "keys": {"root-test": "test-canonical-key-material"},
        }
        pretty_key_ring = json.dumps(key_ring, indent=2)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            previous_runtime = temporary_directory / "runtime.json"
            previous_runtime.write_text(
                json.dumps({"runtime": {"docker_image_reference": image_reference}}),
                encoding="utf-8",
            )

            def render(operation: str, secret_keys_json: str) -> subprocess.CompletedProcess[str]:
                output_file = temporary_directory / "github-output.txt"
                output_file.unlink(missing_ok=True)
                result = subprocess.run(
                    ["bash", "-ceu", render_step.run],
                    check=False,
                    capture_output=True,
                    env={
                        **os.environ,
                        "BOOTSTRAP_SECRET_OPERATION": operation,
                        "DEPLOY_IMAGE_REFERENCE": image_reference,
                        "GITHUB_OUTPUT": str(output_file),
                        "IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                        "LAUNCHPLANE_DOKPLOY_TARGET_ID": "launchplane-target",
                        "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
                        "LAUNCHPLANE_SECRET_KEYS_JSON": secret_keys_json,
                        "OMIT_EVERY_CODE_ENV": "false",
                        "OMIT_NPMPLUS_ENV": "false",
                        "OMIT_OWNER_AGENT_ENV": "false",
                        "OMIT_TERMINAL_AGENT_ENV": "false",
                        "PREVIOUS_RUNTIME_RESPONSE_FILE": str(previous_runtime),
                        "RUNNER_TEMP": str(temporary_directory),
                    },
                    text=True,
                )
                return result

            install = render("install", pretty_key_ring)
            self.assertEqual(install.returncode, 0, install.stderr)
            install_outputs = _read_github_outputs(temporary_directory)
            install_payload = json.loads(
                Path(install_outputs["payload_file"]).read_text(encoding="utf-8")
            )
            installed_value = install_payload["deploy"]["oauth_env"]["LAUNCHPLANE_SECRET_KEYS_JSON"]
            self.assertEqual(json.loads(installed_value), key_ring)
            self.assertNotIn(
                "LAUNCHPLANE_SECRET_KEYS_JSON",
                install_payload["deploy"].get("oauth_env_removals", []),
            )

            preserve = render("preserve", pretty_key_ring)
            self.assertEqual(preserve.returncode, 0, preserve.stderr)
            preserve_outputs = _read_github_outputs(temporary_directory)
            preserve_payload = json.loads(
                Path(preserve_outputs["payload_file"]).read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "LAUNCHPLANE_SECRET_KEYS_JSON",
                preserve_payload["deploy"].get("oauth_env", {}),
            )
            self.assertNotIn(
                "LAUNCHPLANE_SECRET_KEYS_JSON",
                preserve_payload["deploy"].get("oauth_env_removals", []),
            )

            remove = render("remove", pretty_key_ring)
            self.assertEqual(remove.returncode, 0, remove.stderr)
            remove_outputs = _read_github_outputs(temporary_directory)
            remove_payload = json.loads(
                Path(remove_outputs["payload_file"]).read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "LAUNCHPLANE_SECRET_KEYS_JSON",
                remove_payload["deploy"].get("oauth_env", {}),
            )
            self.assertIn(
                "LAUNCHPLANE_SECRET_KEYS_JSON",
                remove_payload["deploy"]["oauth_env_removals"],
            )

            missing = render("install", "")
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires LAUNCHPLANE_SECRET_KEYS_JSON", missing.stderr)

            previous_runtime.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "docker_image_reference": (
                                "ghcr.io/cbusillo/launchplane@sha256:" + ("b" * 64)
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            changed_image = render("install", pretty_key_ring)
            self.assertNotEqual(changed_image.returncode, 0)
            self.assertIn("must retain the current deployed image", changed_image.stderr)

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
            '          })"', 1
        )[0]
        removal_lines = removals_block.splitlines()
        start = next(index for index, line in enumerate(removal_lines) if line.strip() == "'(")
        end = next(
            index
            for index, line in enumerate(removal_lines[start + 1 :], start + 1)
            if line.strip() == ")'"
        )
        removal_lines[start] = removal_lines[start].split("'", 1)[1]
        removal_lines[end] = removal_lines[end].rsplit("'", 1)[0]
        jq_filter = textwrap.dedent("\n".join(removal_lines[start : end + 1]))
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
                    "--arg",
                    "bootstrap_secret_operation",
                    "preserve",
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
        self.assertIn("uses: actions/upload-artifact@", workflow_text)
        self.assertIn("Evidence artifact: launchplane-break-glass-rollback", workflow_text)
        self.assertIn("manual break-glass only", workflow_text)

    def test_deploy_launchplane_routes_dispatch_values_out_of_shell_source(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")
        shell_blocks = re.findall(
            r"(?ms)^        run: \|\n(.*?)(?=^        [a-zA-Z][a-zA-Z_-]*:|^      - |^  [a-zA-Z0-9_-]+:|\Z)",
            workflow_text,
        )
        emergency_job = workflow_text.split("  emergency-dokploy-rollback:\n", 1)[1]

        self.assertGreater(len(shell_blocks), 0)
        self.assertNotIn("${{", "\n".join(shell_blocks))
        self.assertNotIn("git_ref:", workflow_text)
        self.assertGreaterEqual(
            workflow_text.count(
                "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
            ),
            2,
        )
        self.assertGreaterEqual(workflow_text.count("github.ref_type == 'branch'"), 2)
        self.assertIn(
            "BREAK_GLASS_IMAGE_REFERENCE: ${{ inputs.break_glass_image_reference }}",
            emergency_job,
        )
        self.assertIn("BREAK_GLASS_REASON: ${{ inputs.break_glass_reason }}", emergency_job)
        self.assertIn(
            "LAUNCHPLANE_ROLLBACK_IMAGE_REFERENCE: ${{ inputs.break_glass_image_reference }}",
            emergency_job,
        )
        self.assertIn(
            "LAUNCHPLANE_ROLLBACK_REASON: ${{ inputs.break_glass_reason }}",
            emergency_job,
        )
        self.assertIn(
            'printf -- "- Reason: %s\\n" "$LAUNCHPLANE_ROLLBACK_REASON"',
            emergency_job,
        )
        self.assertIn(
            'printf -- "- Rollback image: %s\\n" "$LAUNCHPLANE_ROLLBACK_IMAGE_REFERENCE"',
            emergency_job,
        )

        validation_step = emergency_job.split("- name: Validate manual break-glass request", 1)[
            1
        ].split("- name: Run manual direct Dokploy rollback", 1)[0]
        validation_script = textwrap.dedent(validation_step.split("        run: |\n", 1)[1])
        image_reference = "ghcr.io/cbusillo/launchplane@sha256:" + ("a" * 64)

        with TemporaryDirectory() as temporary_directory:
            command_marker = Path(temporary_directory) / "command-substitution-ran"
            rollback_reason = f'Restore after "review" $(touch {command_marker})'
            subprocess.run(
                ["bash", "-ceu", validation_script],
                check=True,
                env={
                    **os.environ,
                    "AUTHZ_GRANTS_MODE": "none",
                    "AUTHZ_MANAGED_MODE": "none",
                    "BREAK_GLASS_IMAGE_REFERENCE": image_reference,
                    "BREAK_GLASS_REASON": rollback_reason,
                    "GITHUB_REPOSITORY": "cbusillo/launchplane",
                    "LAUNCHPLANE_DOKPLOY_TARGET_ID": "launchplane-target",
                    "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
                    "LAUNCHPLANE_IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                },
                text=True,
            )

            self.assertFalse(command_marker.exists())

    def test_deploy_launchplane_rejects_multiline_previous_image_output(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")
        render_step = workflow_text.split("- name: Render Launchplane self deploy request", 1)[
            1
        ].split("- name: Request Launchplane self deploy", 1)[0]
        render_script = textwrap.dedent(render_step.split("        run: |\n", 1)[1]).split(
            "service_env_json=", 1
        )[0]

        with TemporaryDirectory() as temporary_directory:
            response_file = Path(temporary_directory) / "runtime.json"
            output_file = Path(temporary_directory) / "github-output"
            response_file.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "docker_image_reference": (
                                "ghcr.io/cbusillo/launchplane@sha256:"
                                + ("a" * 64)
                                + "\nprevious_image_reference=attacker-controlled"
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bash", "-ceu", render_script],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "DEPLOY_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:" + ("b" * 64),
                    "GITHUB_OUTPUT": str(output_file),
                    "IMAGE_REPOSITORY": "ghcr.io/cbusillo/launchplane",
                    "PREVIOUS_RUNTIME_RESPONSE_FILE": str(response_file),
                },
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not contain control characters", result.stderr)
            self.assertFalse(output_file.exists())

    def test_deploy_launchplane_break_glass_limits_credentials_and_permissions(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")
        deploy_job = workflow_text.split("  deploy:\n", 1)[1].split(
            "  emergency-dokploy-rollback:\n", 1
        )[0]
        emergency_job = workflow_text.split("  emergency-dokploy-rollback:\n", 1)[1]
        validation_step = emergency_job.split("- name: Validate manual break-glass request", 1)[
            1
        ].split("- name: Run manual direct Dokploy rollback", 1)[0]
        rollback_step = emergency_job.split("- name: Run manual direct Dokploy rollback", 1)[
            1
        ].split("- name: Upload break-glass rollback evidence", 1)[0]

        self.assertIn("environment: launchplane-break-glass", emergency_job)
        self.assertIn("permissions:\n      contents: read", emergency_job)
        self.assertNotIn("id-token:", emergency_job)
        self.assertNotIn("packages:", emergency_job)
        self.assertIn("uses: actions/checkout@", emergency_job)
        self.assertIn("uses: actions/upload-artifact@", emergency_job)
        self.assertNotIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST", deploy_job)
        self.assertNotIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN", deploy_job)
        self.assertNotIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST", validation_step)
        self.assertNotIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN", validation_step)
        self.assertIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST", rollback_step)
        self.assertIn("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN", rollback_step)
        self.assertIn("must use the configured Launchplane image repository", validation_step)
        self.assertIn("between 8 and 500 printable characters", validation_step)

    def test_deploy_workflow_exposes_runtime_key_safety_rule_config(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON", workflow_text)
        self.assertIn("vars.LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON", workflow_text)

    def test_normal_deploy_does_not_mutate_authz_policy(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")
        deploy_job = workflow_text.split("  deploy:\n", 1)[1].split(
            "  operator-authz-managed-validate:\n", 1
        )[0]

        self.assertNotIn("/v1/authz-policies/", deploy_job)
        self.assertNotIn("LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON", deploy_job)
        self.assertIn("id: runtime_key_safety", deploy_job)
        self.assertIn(
            "payload-file: ${{ steps.runtime_key_safety.outputs.runtime_key_safety_policy_file }}",
            deploy_job,
        )
        self.assertIn(
            "idempotency-key: ${{ steps.runtime_key_safety.outputs.runtime_key_safety_idempotency_key }}",
            deploy_job,
        )
        self.assertIn("route-path: /v1/runtime-key-safety/policies/apply", deploy_job)

    def test_deploy_workflow_removes_exact_authz_bridge(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertFalse(Path("scripts/deploy/ensure-authz-grants.sh").exists())
        for retired_token in (
            "operator-authz-managed-validate:",
            "operator-authz-managed:",
            "operator-authz-grants:",
            "authz_managed_mode",
            "authz_managed_reviewed_plan_sha256",
            "authz_managed_reason",
            "authz_managed_related_issue",
            "authz_grants_mode",
            "authz_grants_expected_sha256",
            "authz_policy_expected_sha256",
            "authz_grants_reason",
            "LAUNCHPLANE_AUTHZ_GRANT_MAINTENANCE_JSON",
            "/v1/authz-policies/github-actions/grants",
        ):
            with self.subTest(retired_token=retired_token):
                self.assertNotIn(retired_token, workflow_text)

    def test_deploy_workflow_reads_deployed_runtime_through_shared_request(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("Read deployed Launchplane runtime", workflow_text)
        self.assertIn("id: deployed_runtime", workflow_text)
        self.assertIn("continue-on-error: true", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("route-path: /v1/service/runtime", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn('expected-status: "200"', workflow_text)
        self.assertIn('timeout-ms: "30000"', workflow_text)
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-runtime-smoke.json",
            workflow_text,
        )
        self.assertIn(
            "RUNTIME_STATUS_CODE: ${{ steps.deployed_runtime.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn("RUNTIME_OUTCOME: ${{ steps.deployed_runtime.outcome }}", workflow_text)
        self.assertIn('runtime_status_code="action_failed"', workflow_text)
        self.assertIn("Launchplane runtime smoke failed.", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)

        deployed_smoke_step = workflow_text.split("- name: Capture v2 deployed smoke evidence", 1)[
            1
        ].split("- name: Upload v2 deployed smoke evidence", 1)[0]
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", deployed_smoke_step)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", deployed_smoke_step)
        self.assertNotIn("Authorization: Bearer", deployed_smoke_step)
        self.assertNotIn('--data-urlencode "audience=', deployed_smoke_step)

    def test_deploy_workflow_requests_self_deploy_through_shared_request(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("Read previous Launchplane runtime", workflow_text)
        self.assertIn("id: previous_runtime", workflow_text)
        self.assertIn("Render Launchplane self deploy request", workflow_text)
        self.assertIn("id: self_deploy", workflow_text)
        self.assertIn("Request Launchplane self deploy", workflow_text)
        self.assertIn("id: self_deploy_request", workflow_text)
        self.assertIn("continue-on-error: true", workflow_text)
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-previous-runtime.json",
            workflow_text,
        )
        self.assertIn(
            "payload-file: ${{ steps.self_deploy.outputs.payload_file }}",
            workflow_text,
        )
        self.assertIn("steps.prep.outputs.self_deploy_idempotency_key ||", workflow_text)
        self.assertIn(
            "format('launchplane-self-deploy:{0}:{1}:{2}:db-authz'",
            workflow_text,
        )
        self.assertIn('expected-status: "200,202"', workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn('timeout-ms: "30000"', workflow_text)
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-self-deploy-response.json",
            workflow_text,
        )
        self.assertIn(
            "SELF_DEPLOY_STATUS: ${{ steps.self_deploy_request.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn(
            "SELF_DEPLOY_OUTCOME: ${{ steps.self_deploy_request.outcome }}",
            workflow_text,
        )
        self.assertIn('status_code="action_failed"', workflow_text)
        self.assertIn("Launchplane self deploy request failed with HTTP", workflow_text)
        self.assertIn("Resolve Launchplane deploy wait timeout", workflow_text)
        self.assertIn("id: deploy_wait_timeout", workflow_text)
        self.assertIn("timeout_ms=$((wait_timeout_seconds * 1000))", workflow_text)
        self.assertIn("deadline_epoch=$(($(date +%s) + wait_timeout_seconds))", workflow_text)
        self.assertIn("Wait for deployed Launchplane runtime image", workflow_text)
        self.assertLess(
            workflow_text.index("Wait for deployed Launchplane runtime image"),
            workflow_text.index("Wait for deployed Launchplane health URLs"),
        )
        self.assertLess(
            workflow_text.index("Wait for deployed Launchplane health URLs"),
            workflow_text.index("Verify deployed Launchplane runtime image after health"),
        )
        self.assertIn("poll-until-path: runtime.docker_image_reference", workflow_text)
        self.assertIn(
            "poll-until-value: ${{ steps.image.outputs.image_reference }}",
            workflow_text,
        )
        self.assertIn('poll-retry-on-request-error: "true"', workflow_text)
        self.assertIn('poll-retry-on-unexpected-status: "true"', workflow_text)
        self.assertIn('poll-interval-ms: "5000"', workflow_text)
        self.assertIn(
            "poll-timeout-ms: ${{ steps.deploy_wait_timeout.outputs.timeout_ms }}",
            workflow_text,
        )
        self.assertIn("remaining_timeout_ms=$((remaining_seconds * 1000))", workflow_text)
        self.assertIn(
            "poll-timeout-ms: ${{ steps.deployed_health.outputs.remaining_timeout_ms }}",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-deployed-runtime-wait.json",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-deployed-runtime-final.json",
            workflow_text,
        )

        self_deploy_block = workflow_text.split("- name: Read previous Launchplane runtime", 1)[
            1
        ].split("- name: Render Launchplane rollback request", 1)[0]
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", self_deploy_block)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", self_deploy_block)
        self.assertNotIn("Authorization: Bearer", self_deploy_block)
        self.assertNotIn("Capture failed Launchplane deploy diagnostics", workflow_text)

    def test_deploy_workflow_requests_rollback_through_shared_request(self) -> None:
        workflow_text = Path(".github/workflows/deploy-launchplane.yml").read_text(encoding="utf-8")

        self.assertIn("Render Launchplane rollback request", workflow_text)
        self.assertIn("id: rollback_request", workflow_text)
        self.assertIn("Request Launchplane rollback through service", workflow_text)
        self.assertIn("id: rollback_request_action", workflow_text)
        self.assertIn("continue-on-error: true", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("route-path: /v1/drivers/launchplane/self-deploy", workflow_text)
        self.assertIn(
            "payload-file: ${{ steps.rollback_request.outputs.payload_file }}",
            workflow_text,
        )
        self.assertIn(
            "idempotency-key: launchplane-self-deploy-rollback:${{ "
            "steps.rollback_request.outputs.previous_image_reference }}:${{ "
            "github.run_id }}:${{ github.run_attempt }}",
            workflow_text,
        )
        self.assertIn('expected-status: "200,202"', workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn('timeout-ms: "30000"', workflow_text)
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-self-deploy-rollback-response.json",
            workflow_text,
        )
        self.assertIn(
            "ROLLBACK_STATUS: ${{ steps.rollback_request_action.outputs.status-code }}",
            workflow_text,
        )
        self.assertIn(
            "ROLLBACK_OUTCOME: ${{ steps.rollback_request_action.outcome }}",
            workflow_text,
        )
        self.assertIn('rollback_status="action_failed"', workflow_text)
        self.assertIn("Launchplane rollback failed", workflow_text)
        self.assertIn("Launchplane rollback requested", workflow_text)
        self.assertIn("Launchplane rollback timed out", workflow_text)
        self.assertIn("Rollback request response summary", workflow_text)
        self.assertIn("Resolve Launchplane rollback wait timeout", workflow_text)
        self.assertIn("id: rollback_wait_timeout", workflow_text)
        self.assertIn("Wait for Launchplane rollback health URLs", workflow_text)
        self.assertIn("id: rollback_health", workflow_text)
        self.assertIn("Wait for Launchplane rollback runtime image", workflow_text)
        self.assertIn("id: rollback_runtime", workflow_text)
        self.assertLess(
            workflow_text.index("Wait for Launchplane rollback runtime image"),
            workflow_text.index("Wait for Launchplane rollback health URLs"),
        )
        self.assertLess(
            workflow_text.index("Wait for Launchplane rollback health URLs"),
            workflow_text.index("Verify Launchplane rollback runtime image after health"),
        )
        self.assertIn(
            "poll-until-value: ${{ steps.rollback_request.outputs.previous_image_reference }}",
            workflow_text,
        )
        self.assertIn('poll-retry-on-unexpected-status: "true"', workflow_text)
        self.assertIn(
            "poll-timeout-ms: ${{ steps.rollback_wait_timeout.outputs.timeout_ms }}",
            workflow_text,
        )
        self.assertIn(
            "poll-timeout-ms: ${{ steps.rollback_health.outputs.remaining_timeout_ms }}",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-rollback-runtime-wait.json",
            workflow_text,
        )
        self.assertIn(
            "response-output-file: ${{ runner.temp }}/launchplane-rollback-runtime-final.json",
            workflow_text,
        )
        self.assertIn("Rollback runtime response summary", workflow_text)
        self.assertIn("ROLLBACK_RUNTIME_OUTCOME", workflow_text)

        rollback_request_block = workflow_text.split(
            "- name: Render Launchplane rollback request", 1
        )[1].split("- name: Request Launchplane rollback through service", 1)[0]
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", rollback_request_block)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", rollback_request_block)
        self.assertNotIn("Authorization: Bearer", rollback_request_block)

    def test_product_onboarding_workflow_coordinates_typed_reviewed_flow(self) -> None:
        workflow_path = Path(".github/workflows/product-onboarding.yml")
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = load_workflow(workflow_path)
        apply_workflow_text = Path(
            ".github/workflows/reusable-generic-web-onboarding-apply.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn("mode:", workflow_text)
        self.assertIn("repository:", workflow_text)
        self.assertIn("image_repository:", workflow_text)
        self.assertIn("runtime_port:", workflow_text)
        self.assertIn("APPLY GENERIC WEB ONBOARDING", workflow_text)
        self.assertIn("actions/create-github-app-token@", workflow_text)
        self.assertIn("LAUNCHPLANE_ONBOARDING_GITHUB_APP_CLIENT_ID", workflow_text)
        self.assertIn("permission-contents: read", workflow_text)
        self.assertIn("preview_base_url:", workflow_text)
        self.assertIn("PREVIEW_BASE_URL: ${{ inputs.preview_base_url }}", workflow_text)
        self.assertIn("preview_base_url:$preview_base_url", workflow_text)
        self.assertIn("Preview base URL", workflow_text)
        self.assertIn('gh api "repos/${REPOSITORY}"', workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("/v1/dokploy-targets/setup", workflow_text)
        self.assertIn("/v1/product-onboarding/apply", workflow_text)
        self.assertIn(
            "/v1/authz-policies/managed-rule-sets/generic-web-preview/plan",
            workflow_text,
        )
        self.assertEqual(
            workflow.job_uses("apply"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-generic-web-onboarding-apply.yml@"
            "ddc0533476246a9c4c52094ab1c945e294adb3c9",
        )
        self.assertIn("/v1/authz-policies/managed-rule-sets/reconcile", apply_workflow_text)
        self.assertIn("environment: launchplane-authz-admin", apply_workflow_text)
        self.assertIn("generic-web-onboarding-plan", workflow_text)
        self.assertIn("generic-web-onboarding-apply", apply_workflow_text)
        self.assertIn("reviewed_plan_sha256:$reviewed", apply_workflow_text)
        self.assertIn("resolved_target_id:$target_id", apply_workflow_text)
        self.assertIn("Reviewed onboarding plan digest does not match", apply_workflow_text)
        self.assertIn("Reviewed authz configuration does not match", apply_workflow_text)
        self.assertIn("Reviewed provider plan digest does not match", apply_workflow_text)
        self.assertIn("authz-managed:operator.generic-web-preview", apply_workflow_text)
        self.assertIn("retention-days: 14", workflow_text)
        self.assertIn("Launchplane worker SHA", workflow_text)
        self.assertIn("product: ${{ needs.plan.outputs.product }}", workflow_text)
        self.assertIn('fail-result-paths: ""', apply_workflow_text)
        self.assertNotIn("manifest_base64", workflow_text)
        concurrency = workflow.data["concurrency"]
        assert isinstance(concurrency, dict)
        self.assertEqual(concurrency["group"], "product-onboarding")
        for checked_workflow_text in (workflow_text, apply_workflow_text):
            self.assertNotIn("actions/checkout", checked_workflow_text)
            self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", checked_workflow_text)
            self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", checked_workflow_text)
            self.assertNotIn("Authorization: Bearer", checked_workflow_text)
            self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", checked_workflow_text)
            self.assertNotIn("curl ", checked_workflow_text)

    def test_generic_web_preview_authorization_workflow_owns_rotation_and_retirement(self) -> None:
        workflow_path = Path(".github/workflows/generic-web-preview-authorization.yml")
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = load_workflow(workflow_path)
        apply_workflow_text = Path(
            ".github/workflows/reusable-generic-web-preview-authz-apply.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Generic Web Preview Authorization", workflow_text)
        for operation in ("onboard", "expand", "contract", "retire"):
            self.assertIn(f"- {operation}", workflow_text)
        self.assertIn("operation must be onboard, expand, contract, or retire.", workflow_text)
        self.assertIn("actions/create-github-app-token@", workflow_text)
        self.assertIn("permission-contents: read", workflow_text)
        self.assertIn("Optional owner/name assertion when retiring", workflow_text)
        self.assertIn("repository_match.group(1) if repository_match else ''", workflow_text)
        self.assertIn("repository_match.group(2) if repository_match else ''", workflow_text)
        self.assertIn(
            "/v1/authz-policies/managed-rule-sets/generic-web-preview/plan",
            workflow_text,
        )
        self.assertEqual(
            workflow.job_uses("apply"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-generic-web-preview-authz-apply.yml@"
            "d21d609514404d3420fceb7fe53857ae407d3ff8",
        )
        self.assertIn("environment: launchplane-authz-admin", apply_workflow_text)
        self.assertIn("/v1/authz-policies/managed-rule-sets/reconcile", apply_workflow_text)
        self.assertIn("Reviewed authz configuration does not match", apply_workflow_text)
        self.assertIn("retention-days: 14", workflow_text)
        self.assertNotIn("managed_set_json", workflow_text)
        self.assertNotIn("managed_set_json", apply_workflow_text)

    def test_advanced_product_onboarding_manifest_workflow_remains_available(self) -> None:
        workflow_text = Path(".github/workflows/product-onboarding-manifest.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Product Onboarding Manifest (Advanced)", workflow_text)
        self.assertIn("manifest_base64", workflow_text)
        self.assertIn("APPLY PRODUCT ONBOARDING", workflow_text)
        self.assertIn("stable-lane-repair", workflow_text)
        self.assertIn("APPLY PRODUCT STABLE LANE REPAIR", workflow_text)
        self.assertIn("reviewed_plan_sha256", workflow_text)
        self.assertIn("/v1/product-profiles/stable-lane-repair/apply", workflow_text)
        self.assertIn("environment: launchplane-authz-admin", workflow_text)
        self.assertIn("/v1/product-onboarding/apply", workflow_text)
        self.assertNotIn("/v1/authz-policies/managed-rule-sets/reconcile", workflow_text)

    def test_work_graph_snapshot_validate_uses_shared_launchplane_request(self) -> None:
        workflow_text = Path(".github/workflows/work-graph-snapshot-validate.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn("route-path: /v1/work-graph/snapshot", workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn(
            "response-output-file: launchplane-work-graph-snapshot.json",
            workflow_text,
        )
        self.assertIn(
            "STATUS_CODE: ${{ steps.snapshot_request.outputs.status-code }}", workflow_text
        )
        self.assertIn('if [ "$STATUS_CODE" != "200" ]; then', workflow_text)
        self.assertIn("name: launchplane-work-graph-snapshot", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_dokploy_target_inspect_uses_shared_launchplane_request(self) -> None:
        workflow_text = Path(".github/workflows/dokploy-target-inspect.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("runs-on: ubuntu-latest", workflow_text)
        self.assertIn("Use context/instance or target_type/target_id, not both.", workflow_text)
        self.assertIn("context and instance are both required for tracked inspect.", workflow_text)
        self.assertIn(
            "target_type and target_id are both required for explicit inspect.", workflow_text
        )
        self.assertIn("context/instance or target_type/target_id is required.", workflow_text)
        self.assertIn('echo "route_path=/v1/dokploy-targets/inspect?${query}"', workflow_text)
        self.assertIn(
            "uses: cbusillo/launchplane/.github/actions/launchplane-request@",
            workflow_text,
        )
        self.assertIn("audience: ${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}", workflow_text)
        self.assertIn("method: GET", workflow_text)
        self.assertIn("route-path: ${{ steps.request.outputs.route_path }}", workflow_text)
        self.assertIn('fail-result-paths: ""', workflow_text)
        self.assertIn(
            "response-output-file: dokploy-target-inspect-response.json",
            workflow_text,
        )
        self.assertIn(
            "STATUS_CODE: ${{ steps.inspect_request.outputs.status-code }}", workflow_text
        )
        self.assertIn('if [ "$STATUS_CODE" != "200" ]; then', workflow_text)
        self.assertIn("name: dokploy-target-inspect", workflow_text)
        self.assertIn("if: always()", workflow_text)
        self.assertIn("if-no-files-found: warn", workflow_text)
        self.assertNotIn("actions/checkout", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_URL", workflow_text)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("LAUNCHPLANE_RUNNER_LABEL", workflow_text)
        self.assertNotIn("curl ", workflow_text)

    def test_product_expected_config_workflow_calls_service_route_with_apply_guard(
        self,
    ) -> None:
        workflow_text = Path(".github/workflows/product-expected-config.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("/v1/product-profiles/expected-config/apply", workflow_text)
        self.assertIn("APPLY PRODUCT EXPECTED CONFIG", workflow_text)
        self.assertIn("context is required for this workflow", workflow_text)
        self.assertIn("requirement_kind", workflow_text)
        self.assertIn("managed_secret_binding", workflow_text)
        self.assertIn("runtime_environment_key", workflow_text)
        self.assertIn(
            "product-expected-config:${{ inputs.product }}:${{ inputs.mode }}:${{ github.run_id }}",
            workflow_text,
        )
        self.assertNotIn("github.run_attempt", workflow_text)
        self.assertIn("product-expected-config-result", workflow_text)

    def test_product_preview_tls_workflow_requires_reviewed_plan_for_apply(self) -> None:
        workflow_text = Path(".github/workflows/product-preview-tls.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("/v1/product-profiles/preview-tls/apply", workflow_text)
        self.assertIn("APPLY PRODUCT PREVIEW TLS", workflow_text)
        self.assertIn("reviewed_plan_sha256", workflow_text)
        self.assertIn("dry-run", workflow_text)
        self.assertIn("letsencrypt", workflow_text)
        self.assertIn(
            "product-preview-tls:${PRODUCT}:apply:${GITHUB_RUN_ID}",
            workflow_text,
        )
        self.assertIn('if [ "$MODE" = "apply" ]; then', workflow_text)
        self.assertNotIn("github.run_attempt", workflow_text)
        self.assertIn("product-preview-tls-result", workflow_text)
        self.assertNotIn("actions/checkout", workflow_text)
        self.assertNotIn("Authorization: Bearer", workflow_text)
        self.assertNotIn("curl ", workflow_text)
        self.assertNotIn("| xargs", workflow_text)

    def test_runtime_key_safety_accepts_configured_rules(
        self,
    ) -> None:
        configured_rules = [
            {
                "binding_key": "EXAMPLE_API_TOKEN",
                "secret_class": "testing",
                "allowed_targets": [{"context": "example-testing", "instances": ["testing"]}],
                "description": "Example testing token.",
            }
        ]
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            result = _run_runtime_key_safety_generator(
                temporary_directory,
                extra_env={
                    "LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON": json.dumps(configured_rules)
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            policy_payload = json.loads(
                (
                    temporary_directory / "runtime-key-safety" / "runtime-key-safety-policy.json"
                ).read_text(encoding="utf-8")
            )
            policy_idempotency = _read_github_outputs(temporary_directory)[
                "runtime_key_safety_idempotency_key"
            ]

        self.assertEqual(policy_payload["product"], "launchplane")
        self.assertEqual(policy_payload["source_label"], "deploy:runtime-key-safety-rules")
        self.assertRegex(
            policy_idempotency,
            r"launchplane-runtime-key-safety-rules:test-sha:[0-9a-f]{64}",
        )
        self.assertNotIn("EXAMPLE_API_TOKEN", policy_idempotency)
        self.assertEqual(policy_payload["rules"][0]["binding_key"], "EXAMPLE_API_TOKEN")
        self.assertEqual(policy_payload["rules"][0]["secret_class"], "testing")
        self.assertEqual(
            policy_payload["rules"][0]["allowed_targets"],
            [{"context": "example-testing", "instances": ["testing"]}],
        )
        self.assertEqual(policy_payload["rules"][0]["description"], "Example testing token.")

    def test_runtime_key_safety_rejects_incomplete_rules(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            result = _run_runtime_key_safety_generator(
                temporary_directory,
                extra_env={
                    "LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON": json.dumps(
                        [{"binding_key": "EXAMPLE_API_TOKEN"}]
                    )
                },
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("binding_key and secret_class", result.stderr)

    def test_runtime_key_safety_rejects_unknown_secret_class(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            result = _run_runtime_key_safety_generator(
                temporary_directory,
                extra_env={
                    "LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON": json.dumps(
                        [{"binding_key": "EXAMPLE_API_TOKEN", "secret_class": "production"}]
                    )
                },
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret_class must be one of", result.stderr)

    def test_product_driver_prod_promotion_supports_odoo_run_contract(self) -> None:
        workflow_text = Path(
            ".github/workflows/reusable-product-driver-prod-promotion.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("default: verireel", workflow_text)
        self.assertIn("Unsupported driver: $DRIVER", workflow_text)
        self.assertIn('required_inputs="PRODUCT CONTEXT FROM_INSTANCE TO_INSTANCE"', workflow_text)
        self.assertIn("Odoo prod promotion requires testing -> prod.", workflow_text)
        self.assertIn('route_path="/v1/drivers/odoo/prod-promotion-run"', workflow_text)
        self.assertIn('idempotency_key="opp:$CONTEXT:$run_scope"', workflow_text)
        self.assertIn("run.context=${{ steps.request.outputs.context }}", workflow_text)
        self.assertIn("run.request_id=${{ steps.request.outputs.request_id }}", workflow_text)
        self.assertIn("result.run_status", workflow_text)
        self.assertIn("result.promotion_status", workflow_text)
        self.assertIn("result.deployment_status", workflow_text)
        self.assertIn("result.post_deploy_status", workflow_text)
        self.assertIn("result.destination_health_status", workflow_text)
        self.assertNotIn('PRODUCT="${GITHUB_REPOSITORY#*/}"\n            CONTEXT=', workflow_text)

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
            existing_runtime_record = store.list_runtime_environment_records()[0]
            store.write_runtime_environment_record(
                existing_runtime_record.model_copy(
                    update={
                        "env": {
                            **existing_runtime_record.env,
                            "UNRELATED_PREVIEW_SETTING": "preserve-me",
                        }
                    }
                )
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
        self.assertEqual(profile.preview.domain_certificate_type, "letsencrypt")
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
        self.assertEqual(
            runtime_records[0].env["UNRELATED_PREVIEW_SETTING"],
            "preserve-me",
        )
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

    def test_apply_product_onboarding_manifest_rejects_historical_context_reactivation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            initial_manifest = ProductOnboardingManifest.model_validate(_manifest_payload())
            initial_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=initial_manifest,
                updated_at="2026-05-03T00:20:00Z",
            )
            store.write_product_profile_record(
                initial_result.product_profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"context": "example-site"})
                            if lane.instance == "testing"
                            else lane
                            for lane in initial_result.product_profile.lanes
                        ),
                        "historical_contexts": ("example-site-testing",),
                        "updated_at": "2026-05-03T01:20:00Z",
                        "source": "test:cutover",
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "cannot reuse historical contexts"):
                apply_product_onboarding_manifest(
                    record_store=store,
                    manifest=initial_manifest,
                    updated_at="2026-05-03T02:20:00Z",
                )
            profile = store.read_product_profile_record("example-site")
            store.close()

        self.assertEqual(
            next(lane.context for lane in profile.lanes if lane.instance == "testing"),
            "example-site",
        )

    def test_product_onboarding_rejects_cross_route_provider_target_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="canonical-site",
                    instance="testing",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="app-testing-123",
                    display_name="canonical-site",
                    provider_target_type="application",
                    provider_evidence={"project_name": "example-site"},
                    updated_at="2026-05-03T00:20:00Z",
                    source_label="test:canonical",
                )
            )
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())

            with self.assertRaisesRegex(ValueError, "already bound to another route"):
                apply_product_onboarding_manifest(record_store=store, manifest=manifest)
            self.assertEqual(store.list_product_profile_records(), ())
            store.close()

    def test_product_onboarding_allows_canonical_repair_from_historical_alias(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest_payload = _manifest_payload()
            lanes = cast(list[dict[str, object]], manifest_payload["lanes"])
            provider_targets = cast(list[dict[str, object]], manifest_payload["provider_targets"])
            runtime_environments = cast(
                list[dict[str, object]], manifest_payload["runtime_environments"]
            )
            secret_bindings = cast(list[dict[str, object]], manifest_payload["secret_bindings"])
            expected_config = cast(dict[str, object], manifest_payload["expected_config"])
            runtime_requirements = cast(
                list[dict[str, object]], expected_config["runtime_environment_keys"]
            )
            secret_requirements = cast(
                list[dict[str, object]], expected_config["managed_secret_bindings"]
            )
            lanes[0]["context"] = "example-site"
            provider_targets[0]["context"] = "example-site"
            for runtime_record in runtime_environments:
                if runtime_record["context"] == "example-site-testing":
                    runtime_record["context"] = "example-site"
            for secret_binding in secret_bindings:
                if secret_binding["context"] == "example-site-testing":
                    secret_binding["context"] = "example-site"
            for runtime_requirement in runtime_requirements:
                if runtime_requirement["context"] == "example-site-testing":
                    runtime_requirement["context"] = "example-site"
            for secret_requirement in secret_requirements:
                if secret_requirement["context"] == "example-site-testing":
                    secret_requirement["context"] = "example-site"
            manifest = ProductOnboardingManifest.model_validate(manifest_payload)
            initial_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T00:20:00Z",
            )
            historical_alias = "example-site-testing"
            store.write_product_profile_record(
                initial_result.product_profile.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"context": historical_alias})
                            if lane.instance == "testing"
                            else lane
                            for lane in initial_result.product_profile.lanes
                        ),
                        "historical_contexts": (historical_alias,),
                        "updated_at": "2026-05-03T01:20:00Z",
                        "source": "test:regression",
                    }
                )
            )
            store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=ProviderTargetRecord(
                                context=historical_alias,
                                instance="testing",
                                provider_id="dokploy",
                                target_category="application",
                                target_id="app-testing-123",
                                display_name="example-site-testing",
                                provider_target_type="application",
                                provider_evidence={"project_name": "example-site"},
                                updated_at="2026-05-03T01:20:00Z",
                                source_label="test:regression",
                            ),
                            expected_absent=True,
                            allowed_conflicting_routes=(("example-site", "testing"),),
                        ),
                    )
                )
            )

            repaired = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
                updated_at="2026-05-03T02:20:00Z",
            )
            store.close()

        testing_lane = next(
            lane for lane in repaired.product_profile.lanes if lane.instance == "testing"
        )
        self.assertEqual(testing_lane.context, "example-site")
        self.assertEqual(repaired.product_profile.historical_contexts, (historical_alias,))

    def test_product_onboarding_rejects_historical_alias_rebind_to_noncanonical_route(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest_payload = _manifest_payload()
            manifest_payload["product"] = "example-site"
            lanes = cast(list[dict[str, object]], manifest_payload["lanes"])
            provider_targets = cast(list[dict[str, object]], manifest_payload["provider_targets"])
            runtime_environments = cast(
                list[dict[str, object]], manifest_payload["runtime_environments"]
            )
            secret_bindings = cast(list[dict[str, object]], manifest_payload["secret_bindings"])
            expected_config = cast(dict[str, object], manifest_payload["expected_config"])
            runtime_requirements = cast(
                list[dict[str, object]], expected_config["runtime_environment_keys"]
            )
            secret_requirements = cast(
                list[dict[str, object]], expected_config["managed_secret_bindings"]
            )
            lanes[0]["context"] = "example-site-new"
            provider_targets[0]["context"] = "example-site-new"
            for runtime_record in runtime_environments:
                if runtime_record["context"] == "example-site-testing":
                    runtime_record["context"] = "example-site-new"
            for secret_binding in secret_bindings:
                if secret_binding["context"] == "example-site-testing":
                    secret_binding["context"] = "example-site-new"
            for runtime_requirement in runtime_requirements:
                if runtime_requirement["context"] == "example-site-testing":
                    runtime_requirement["context"] = "example-site-new"
            for secret_requirement in secret_requirements:
                if secret_requirement["context"] == "example-site-testing":
                    secret_requirement["context"] = "example-site-new"
            manifest = ProductOnboardingManifest.model_validate(manifest_payload)
            store.write_product_profile_record(
                build_product_profile_record(
                    manifest=manifest,
                    updated_at="2026-05-03T00:20:00Z",
                ).model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"context": "example-site-testing"})
                            if lane.instance == "testing"
                            else lane
                            for lane in build_product_profile_record(
                                manifest=manifest,
                                updated_at="2026-05-03T00:20:00Z",
                            ).lanes
                        ),
                        "historical_contexts": ("example-site-testing",),
                    }
                )
            )
            store.write_provider_target_record(
                ProviderTargetRecord(
                    context="example-site-testing",
                    instance="testing",
                    provider_id="dokploy",
                    target_category="application",
                    target_id="app-testing-123",
                    display_name="example-site-testing",
                    provider_target_type="application",
                    provider_evidence={"project_name": "example-site"},
                    updated_at="2026-05-03T00:20:00Z",
                    source_label="test:regression",
                )
            )

            with self.assertRaisesRegex(ValueError, "already bound to another route"):
                apply_product_onboarding_manifest(record_store=store, manifest=manifest)
            store.close()

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

    def test_product_onboarding_manifest_rejects_missing_image_repository(
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
                    "target_name": "cm-repairshopr-sync",
                    "healthcheck_enabled": False,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "requires image_repository"):
            ProductOnboardingManifest.model_validate(payload)

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

    def test_product_onboarding_manifest_rejects_enabled_target_healthcheck_without_path(
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
                    "health_monitoring": {"checks": []},
                }
            ],
            "provider_targets": [
                {
                    "context": "repairshopr-sync",
                    "instance": "prod",
                    "target_id": "app-123",
                    "target_type": "application",
                    "healthcheck_enabled": True,
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "healthcheck requires"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_rejects_generic_web_source_backed_target(
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

        with self.assertRaisesRegex(ValueError, "no longer accepts source-backed"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_manifest_normalizes_driver_id_before_source_backed_guard(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "repairshopr-sync",
            "display_name": "RepairShopr Sync",
            "repository": "cbusillo/repairshopr_api",
            "driver_id": " generic-web ",
            "image_repository": "ghcr.io/cbusillo/repairshopr-sync",
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

        with self.assertRaisesRegex(ValueError, "no longer accepts source-backed"):
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
                        "monitoring_intent": "public",
                        "checks": [{"name": "public-ingress", "kind": "public_http"}],
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
                        "monitoring_intent": "public",
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
                        ],
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
                        "monitoring_intent": "public",
                        "checks": [
                            {"name": "api check", "kind": "public_http"},
                            {
                                "name": "api-check",
                                "kind": "private_http",
                                "private_endpoint_key": "example-site-prod-runtime",
                            },
                        ],
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
                        "monitoring_intent": "private",
                        "checks": [
                            {"name": "public-ingress", "kind": "public_http"},
                            {
                                "name": "private-runtime",
                                "kind": "private_http",
                                "private_endpoint_key": "example-site-prod-runtime",
                            },
                        ],
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
                    "health_monitoring": {
                        "monitoring_intent": "public",
                        "checks": [{"name": "---", "kind": "public_http"}],
                    },
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
                        "monitoring_intent": "public",
                        "checks": [{"name": "public-ingress", "kind": "public_http"}],
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
                    "target_id": "app-123",
                    "target_type": "application",
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
                    "target_id": "app-123",
                    "target_type": "application",
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
                    "--allow-direct-db-mutation",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["product"], "example-site")
        self.assertEqual(payload["secret_binding_count"], 1)
        self.assertNotIn("secret_id", payload["secret_bindings"][0])

    def test_product_onboarding_cli_requires_direct_db_acknowledgement(self) -> None:
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

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Direct local DB mutation is restricted", result.output)


if __name__ == "__main__":
    unittest.main()
