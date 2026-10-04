import importlib.util
import json
import os
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
                        "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_RUN_ID": "12345",
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
            self.assertEqual(
                outputs["forward_deployment_marker"],
                "github-actions:12345:2:deploy",
            )
            self.assertEqual(
                outputs["rollback_deployment_marker"],
                "github-actions:12345:2:rollback",
            )

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
                        "PATH": os.environ["PATH"],
                        "HOME": str(temporary_directory),
                        "BOOTSTRAP_SECRET_OPERATION": operation,
                        "ORDINARY_AGENT_WORKERS": "preserve",
                        "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "absent",
                        "DEPLOYMENT_MARKER": "github-actions:12345:2:deploy",
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
            self.assertEqual(
                install_payload["deploy"]["oauth_env"]["LAUNCHPLANE_DEPLOYMENT_MARKER"],
                "github-actions:12345:2:deploy",
            )
            self.assertEqual(
                install_payload["deploy"]["oauth_env_expected_absent"],
                ["LAUNCHPLANE_SECRET_KEYS_JSON"],
            )
            self.assertNotIn("oauth_env_expected_values", install_payload["deploy"])
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
            self.assertNotIn(
                "LAUNCHPLANE_DEPLOYMENT_MARKER",
                preserve_payload["deploy"].get("oauth_env", {}),
            )
            self.assertNotIn("oauth_env_expected_absent", preserve_payload["deploy"])
            self.assertNotIn("oauth_env_expected_values", preserve_payload["deploy"])

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
            self.assertEqual(
                remove_payload["deploy"]["oauth_env"]["LAUNCHPLANE_DEPLOYMENT_MARKER"],
                "github-actions:12345:2:deploy",
            )
            self.assertEqual(
                json.loads(
                    remove_payload["deploy"]["oauth_env_expected_values"][
                        "LAUNCHPLANE_SECRET_KEYS_JSON"
                    ]
                ),
                key_ring,
            )
            self.assertNotIn("oauth_env_expected_absent", remove_payload["deploy"])

            missing = render("install", "")
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires LAUNCHPLANE_SECRET_KEYS_JSON", missing.stderr)

            missing_remove = render("remove", "")
            self.assertNotEqual(missing_remove.returncode, 0)
            self.assertIn("requires LAUNCHPLANE_SECRET_KEYS_JSON", missing_remove.stderr)

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

    def test_deploy_launchplane_renders_exact_bootstrap_secret_rollback(self) -> None:
        workflow = load_workflow(".github/workflows/deploy-launchplane.yml")
        rollback_step = workflow.step_named("deploy", "Render Launchplane rollback request")
        self.assertIsNotNone(rollback_step)
        assert rollback_step is not None
        image_reference = "ghcr.io/cbusillo/launchplane@sha256:" + ("a" * 64)
        key_ring = {
            "active_key_id": "root-test",
            "keys": {"root-test": "test-canonical-key-material"},
        }
        pretty_key_ring = json.dumps(key_ring, indent=2)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            rollback_payload_path = (
                temporary_directory / "launchplane-self-deploy-rollback-payload.json"
            )

            def render_rollback(operation: str) -> subprocess.CompletedProcess[str]:
                output_file = temporary_directory / "github-output.txt"
                output_file.unlink(missing_ok=True)
                rollback_payload_path.unlink(missing_ok=True)
                return subprocess.run(
                    ["bash", "-ceu", rollback_step.run],
                    check=False,
                    capture_output=True,
                    env={
                        "PATH": os.environ["PATH"],
                        "HOME": str(temporary_directory),
                        "BOOTSTRAP_SECRET_OPERATION": operation,
                        "ORDINARY_AGENT_WORKERS": "preserve",
                        "ORDINARY_AGENT_WORKERS_EXPECTED_STATE": "absent",
                        "DEPLOYED_IMAGE_REFERENCE": image_reference,
                        "FORWARD_DEPLOYMENT_MARKER": "github-actions:12345:2:deploy",
                        "GITHUB_OUTPUT": str(output_file),
                        "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_RUN_ID": "12345",
                        "LAUNCHPLANE_DOKPLOY_TARGET_ID": "launchplane-target",
                        "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
                        "LAUNCHPLANE_SECRET_KEYS_JSON": pretty_key_ring,
                        "PREVIOUS_IMAGE_REFERENCE": image_reference,
                        "ROLLBACK_DEPLOYMENT_MARKER": "github-actions:12345:2:rollback",
                        "RUNNER_TEMP": str(temporary_directory),
                        "SELF_DEPLOY_IDEMPOTENCY_KEY": (
                            "launchplane-self-deploy:issue-2249:bootstrap-rollback:v1"
                        ),
                    },
                    text=True,
                )

            install = render_rollback("install")
            self.assertEqual(install.returncode, 0, install.stderr)
            install_outputs = _read_github_outputs(temporary_directory)
            install_payload = json.loads(
                Path(install_outputs["payload_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(install_payload["deploy"]["image_reference"], image_reference)
            self.assertEqual(
                install_payload["deploy"]["oauth_env"],
                {"LAUNCHPLANE_DEPLOYMENT_MARKER": "github-actions:12345:2:rollback"},
            )
            self.assertEqual(
                install_payload["deploy"]["oauth_env_removals"],
                ["LAUNCHPLANE_SECRET_KEYS_JSON"],
            )
            self.assertEqual(
                install_payload["deploy"]["oauth_env_expected_values"][
                    "LAUNCHPLANE_DEPLOYMENT_MARKER"
                ],
                "github-actions:12345:2:deploy",
            )
            self.assertEqual(
                json.loads(
                    install_payload["deploy"]["oauth_env_expected_values"][
                        "LAUNCHPLANE_SECRET_KEYS_JSON"
                    ]
                ),
                key_ring,
            )
            self.assertNotIn("oauth_env_expected_absent", install_payload["deploy"])
            self.assertEqual(
                install_outputs["deployment_marker"],
                "github-actions:12345:2:rollback",
            )
            self.assertTrue(
                install_outputs["idempotency_key"].startswith("launchplane-self-deploy-rollback:")
            )

            remove = render_rollback("remove")
            self.assertEqual(remove.returncode, 0, remove.stderr)
            remove_outputs = _read_github_outputs(temporary_directory)
            remove_payload = json.loads(
                Path(remove_outputs["payload_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                json.loads(remove_payload["deploy"]["oauth_env"]["LAUNCHPLANE_SECRET_KEYS_JSON"]),
                key_ring,
            )
            self.assertEqual(
                remove_payload["deploy"]["oauth_env"]["LAUNCHPLANE_DEPLOYMENT_MARKER"],
                "github-actions:12345:2:rollback",
            )
            self.assertEqual(
                remove_payload["deploy"]["oauth_env_expected_absent"],
                ["LAUNCHPLANE_SECRET_KEYS_JSON"],
            )
            self.assertEqual(
                remove_payload["deploy"]["oauth_env_expected_values"],
                {"LAUNCHPLANE_DEPLOYMENT_MARKER": "github-actions:12345:2:deploy"},
            )
            self.assertNotIn("oauth_env_removals", remove_payload["deploy"])

            preserve = render_rollback("preserve")
            self.assertEqual(preserve.returncode, 0, preserve.stderr)
            self.assertFalse(rollback_payload_path.exists())

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
