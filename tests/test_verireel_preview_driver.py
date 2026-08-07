import base64
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
    RuntimeSecretSafetyTargetScope,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.dokploy import DokployTargetDefinition
from control_plane.workflows.verireel_preview_driver import VeriReelPreviewDestroyRequest
from control_plane.workflows.verireel_preview_driver import VeriReelPreviewRefreshRequest
from control_plane.workflows.verireel_preview_driver import VeriReelPreviewRefreshTransportError
from control_plane.workflows.verireel_preview_driver import _build_preview_database_command
from control_plane.workflows.verireel_preview_driver import (
    _enforce_verireel_preview_runtime_key_safety,
)
from control_plane.workflows.verireel_preview_driver import _ensure_application
from control_plane.workflows.verireel_preview_driver import _preview_database_admin_module_source
from control_plane.workflows.verireel_preview_driver import _resolve_preview_secret
from control_plane.workflows.verireel_preview_driver import _resolve_preview_url
from control_plane.workflows.verireel_preview_driver import _run_application_command
from control_plane.workflows.verireel_preview_driver import _run_application_command_with_retries
from control_plane.workflows.verireel_preview_driver import _verireel_template_runtime_secret_keys
from control_plane.workflows.verireel_preview_driver import execute_verireel_preview_refresh


class _RuntimeKeySafetyStore:
    def __init__(
        self,
        *,
        policies: tuple[RuntimeKeySafetyPolicyRecord, ...] = (),
        bindings: tuple[SecretBinding, ...] = (),
    ) -> None:
        self.policies = policies
        self.bindings = bindings

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(record for record in self.policies if not status or record.status == status)
        if limit is not None:
            return records[:limit]
        return records

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        if limit is not None:
            return bindings[:limit]
        return bindings


def _runtime_policy(
    *, secret_class: RuntimeSecretClass = "preview"
) -> RuntimeKeySafetyPolicyRecord:
    return RuntimeKeySafetyPolicyRecord(
        record_id="runtime-key-safety-policy-test",
        status="active",
        source="test",
        updated_at="2026-05-05T22:15:00Z",
        rules=(
            RuntimeSecretSafetyRule(
                binding_key="DATABASE_URL",
                secret_class=secret_class,
                allowed_contexts=("verireel-testing",),
            ),
            RuntimeSecretSafetyRule(
                binding_key="BETTER_AUTH_SECRET",
                secret_class=secret_class,
                allowed_contexts=("verireel-testing",),
            ),
        ),
    )


def _verireel_preview_runtime_policy() -> RuntimeKeySafetyPolicyRecord:
    return RuntimeKeySafetyPolicyRecord(
        record_id="runtime-key-safety-policy-verireel-preview-test",
        status="active",
        source="test",
        updated_at="2026-05-05T22:15:00Z",
        rules=(
            RuntimeSecretSafetyRule(
                binding_key="POSTGRES_PASSWORD",
                secret_class="shared_safe",
                allowed_targets=(
                    RuntimeSecretSafetyTargetScope(
                        context="verireel-testing",
                        instance_patterns=("pr-*",),
                    ),
                ),
            ),
            RuntimeSecretSafetyRule(
                binding_key="BETTER_AUTH_SECRET",
                secret_class="shared_safe",
                allowed_targets=(
                    RuntimeSecretSafetyTargetScope(
                        context="verireel-testing",
                        instance_patterns=("pr-*",),
                    ),
                ),
            ),
            RuntimeSecretSafetyRule(
                binding_key="VERIREEL_SECRETS_MASTER_KEY",
                secret_class="shared_safe",
                allowed_targets=(
                    RuntimeSecretSafetyTargetScope(
                        context="verireel-testing",
                        instance_patterns=("pr-*",),
                    ),
                ),
            ),
            RuntimeSecretSafetyRule(
                binding_key="VERIREEL_CRON_SECRET",
                secret_class="shared_safe",
                allowed_targets=(
                    RuntimeSecretSafetyTargetScope(
                        context="verireel-testing",
                        instance_patterns=("pr-*",),
                    ),
                ),
            ),
        ),
    )


def _runtime_binding(binding_key: str) -> SecretBinding:
    normalized_key = binding_key.lower().replace("_", "-")
    return SecretBinding(
        binding_id=f"secret-{normalized_key}-binding-{normalized_key}",
        secret_id=f"secret-{normalized_key}",
        integration="runtime_environment",
        binding_key=binding_key,
        context="verireel",
        instance="testing",
        status="configured",
        created_at="2026-05-05T22:15:00Z",
        updated_at="2026-05-05T22:15:00Z",
    )


def _template_target() -> DokployTargetDefinition:
    return DokployTargetDefinition(
        context="verireel",
        instance="testing",
        target_type="application",
        target_id="app-template",
        target_name="verireel-testing",
    )


def _refresh_request() -> VeriReelPreviewRefreshRequest:
    return VeriReelPreviewRefreshRequest.model_validate(
        {
            "anchor_pr_number": 71,
            "anchor_pr_url": "https://github.com/every/verireel/pull/71",
            "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
            "preview_slug": "pr-71",
            "preview_url": "https://pr-71.ver-preview.shinycomputers.com",
            "image_reference": "ghcr.io/every/verireel-app:pr-71-sha-6b3c9d7",
        }
    )


class VeriReelPreviewDriverTests(unittest.TestCase):
    def test_ensure_application_uses_default_server_when_template_omits_server_id(self) -> None:
        requests: list[dict[str, object]] = []

        def _create_application(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {"applicationId": "app-preview"}

        with (
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.dokploy_request",
                side_effect=_create_application,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._fetch_application",
                return_value={"applicationId": "app-preview"},
            ),
        ):
            application = _ensure_application(
                host="https://dokploy.example",
                token="token",
                application_name="pr-71",
                app_name="pr-71",
                description="Launchplane preview",
                template_application={"environmentId": "env-1"},
            )

        self.assertEqual(application, {"applicationId": "app-preview"})
        self.assertEqual(requests[0]["path"], "/api/application.create")
        self.assertEqual(
            requests[0]["payload"],
            {
                "name": "pr-71",
                "appName": "pr-71",
                "description": "Launchplane preview",
                "environmentId": "env-1",
            },
        )

    def test_ensure_application_forwards_template_server_id(self) -> None:
        requests: list[dict[str, object]] = []

        def _create_application(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {"applicationId": "app-preview"}

        with (
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.dokploy_request",
                side_effect=_create_application,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._fetch_application",
                return_value={"applicationId": "app-preview"},
            ),
        ):
            _ensure_application(
                host="https://dokploy.example",
                token="token",
                application_name="pr-71",
                app_name="pr-71",
                description="Launchplane preview",
                template_application={"environmentId": "env-1", "serverId": "server-1"},
            )

        self.assertEqual(
            requests[0]["payload"],
            {
                "name": "pr-71",
                "appName": "pr-71",
                "description": "Launchplane preview",
                "environmentId": "env-1",
                "serverId": "server-1",
            },
        )

    def test_build_preview_database_command_uses_bundled_temp_files(self) -> None:
        command = _build_preview_database_command(
            action="ensure",
            admin_database_url="postgresql://user:pass@host:5432/postgres",
            database_name="verireel_preview_pr_71",
            role_name="verireel_preview_pr_71",
            password="secret",
        )

        self.assertIn("PREVIEW_DB_ARGS_BASE64=", command)
        self.assertIn("/tmp/.preview-db-admin-", command)
        self.assertIn("/tmp/.preview-db-admin-runner-", command)
        self.assertIn('node "$temp_runner" "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_script"', command)
        self.assertIn('base64 -d > "$temp_runner"', command)
        self.assertIn('rm -f "$temp_script" "$temp_runner" || true', command)

        parse_result = subprocess.run(["sh", "-n", "-c", command], check=False)
        self.assertEqual(parse_result.returncode, 0)

    def test_preview_database_admin_module_source_is_valid_javascript(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            module_path = Path(temporary_directory_name) / "preview-db-admin.mjs"
            module_path.write_text(_preview_database_admin_module_source(), encoding="utf-8")

            parse_result = subprocess.run(["node", "--check", str(module_path)], check=False)

        self.assertEqual(parse_result.returncode, 0)

    def test_preview_database_admin_module_source_prefers_force_drop(self) -> None:
        source = _preview_database_admin_module_source()

        self.assertIn("WITH (FORCE)", source)

    def test_preview_refresh_request_requires_pr_scoped_preview_slug(self) -> None:
        with self.assertRaisesRegex(ValueError, "preview_slug to match anchor_pr_number"):
            VeriReelPreviewRefreshRequest.model_validate(
                {
                    "anchor_pr_number": 71,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/71",
                    "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                    "preview_slug": "pr-72",
                    "preview_url": "https://pr-71.ver-preview.shinycomputers.com",
                    "image_reference": "ghcr.io/every/verireel-app:pr-71-sha-6b3c9d7",
                }
            )

    def test_preview_refresh_can_derive_preview_url_from_runtime_records(self) -> None:
        request = VeriReelPreviewRefreshRequest.model_validate(
            {
                "anchor_pr_number": 71,
                "anchor_pr_url": "https://github.com/every/verireel/pull/71",
                "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                "preview_slug": "pr-71",
                "image_reference": "ghcr.io/every/verireel-app:pr-71-sha-6b3c9d7",
            }
        )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.control_plane_runtime_environments.resolve_runtime_context_values",
                return_value={
                    "LAUNCHPLANE_PREVIEW_BASE_URL": "https://ver-preview.shinycomputers.com"
                },
            ) as resolve_values,
        ):
            preview_url = _resolve_preview_url(
                control_plane_root=Path(temporary_directory_name),
                request=request,
            )

        self.assertEqual(preview_url, "https://pr-71.ver-preview.shinycomputers.com")
        resolve_values.assert_called_once_with(
            control_plane_root=Path(temporary_directory_name),
            context_name="verireel-testing",
        )

    def test_verireel_preview_runtime_key_safety_rejects_missing_store(self) -> None:
        with self.assertRaisesRegex(click.ClickException, "database storage"):
            _enforce_verireel_preview_runtime_key_safety(
                record_store=None,
                template_target=_template_target(),
                template_env_map={"EXTERNAL_API_TOKEN": "api-token"},
                request=_refresh_request(),
            )

    def test_verireel_template_runtime_secret_keys_skip_rewritten_database_url(self) -> None:
        self.assertEqual(
            _verireel_template_runtime_secret_keys(
                {
                    "DATABASE_URL": "postgresql://user:pass@db.example/verireel_testing",
                    "BETTER_AUTH_SECRET": "auth-secret",
                    "VERIREEL_SECRETS_MASTER_KEY": "master-key",
                    "VERIREEL_CRON_SECRET": "cron-secret",
                    "VERIREEL_SMOKE_MAINTENANCE_SECRET": "smoke-secret",
                    "NEXT_PUBLIC_SITE_URL": "https://testing.example",
                }
            ),
            (),
        )

    def test_verireel_preview_runtime_key_safety_ignores_generated_preview_secrets(
        self,
    ) -> None:
        _enforce_verireel_preview_runtime_key_safety(
            record_store=None,
            template_target=_template_target(),
            template_env_map={
                "DATABASE_URL": "postgresql://user:pass@db.example/verireel_testing",
                "BETTER_AUTH_SECRET": "auth-secret",
                "VERIREEL_SECRETS_MASTER_KEY": "master-key",
                "VERIREEL_CRON_SECRET": "cron-secret",
                "VERIREEL_SMOKE_MAINTENANCE_SECRET": "smoke-secret",
            },
            request=_refresh_request(),
        )

    def test_resolve_preview_secret_rotates_copied_template_values(self) -> None:
        self.assertEqual(
            _resolve_preview_secret(
                existing_env_map={"VERIREEL_SMOKE_MAINTENANCE_SECRET": "template"},
                key="VERIREEL_SMOKE_MAINTENANCE_SECRET",
                generate=lambda: "generated",
                template_env_map={"VERIREEL_SMOKE_MAINTENANCE_SECRET": "template"},
            ),
            "generated",
        )
        self.assertEqual(
            _resolve_preview_secret(
                existing_env_map={"VERIREEL_SMOKE_MAINTENANCE_SECRET": "preview"},
                key="VERIREEL_SMOKE_MAINTENANCE_SECRET",
                generate=lambda: "generated",
                template_env_map={"VERIREEL_SMOKE_MAINTENANCE_SECRET": "template"},
            ),
            "preview",
        )

    def test_verireel_preview_runtime_key_safety_requires_bindings_for_other_copied_secrets(
        self,
    ) -> None:
        with self.assertRaisesRegex(click.ClickException, "binding_missing"):
            _enforce_verireel_preview_runtime_key_safety(
                record_store=_RuntimeKeySafetyStore(policies=(_runtime_policy(),)),
                template_target=_template_target(),
                template_env_map={"EXTERNAL_API_TOKEN": "api-token"},
                request=_refresh_request(),
            )

    def test_verireel_preview_runtime_key_safety_blocks_prod_only_template_secret(
        self,
    ) -> None:
        store = _RuntimeKeySafetyStore(
            policies=(
                RuntimeKeySafetyPolicyRecord(
                    record_id="runtime-key-safety-policy-prod-only-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-05T22:15:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="EXTERNAL_API_TOKEN",
                            secret_class="prod_only",
                            allowed_contexts=("verireel-testing",),
                        ),
                    ),
                ),
            ),
            bindings=(_runtime_binding("EXTERNAL_API_TOKEN"),),
        )

        with self.assertRaisesRegex(click.ClickException, "secret_class_not_allowed"):
            _enforce_verireel_preview_runtime_key_safety(
                record_store=store,
                template_target=_template_target(),
                template_env_map={"EXTERNAL_API_TOKEN": "api-token"},
                request=_refresh_request(),
            )

    def test_verireel_preview_runtime_key_safety_allows_preview_template_secrets(
        self,
    ) -> None:
        store = _RuntimeKeySafetyStore(
            policies=(_runtime_policy(),),
            bindings=(
                _runtime_binding("DATABASE_URL"),
                _runtime_binding("BETTER_AUTH_SECRET"),
            ),
        )

        _enforce_verireel_preview_runtime_key_safety(
            record_store=store,
            template_target=_template_target(),
            template_env_map={
                "DATABASE_URL": "postgresql://user:pass@db.example/verireel_testing",
                "BETTER_AUTH_SECRET": "auth-secret",
                "NEXT_PUBLIC_SITE_URL": "https://testing.example",
            },
            request=_refresh_request(),
        )

    def test_verireel_preview_runtime_key_safety_allows_pr_pattern_for_shared_template_secrets(
        self,
    ) -> None:
        store = _RuntimeKeySafetyStore(
            policies=(_verireel_preview_runtime_policy(),),
            bindings=(
                _runtime_binding("POSTGRES_PASSWORD"),
                _runtime_binding("BETTER_AUTH_SECRET"),
                _runtime_binding("VERIREEL_SECRETS_MASTER_KEY"),
                _runtime_binding("VERIREEL_CRON_SECRET"),
                _runtime_binding("VERIREEL_SMOKE_MAINTENANCE_SECRET"),
            ),
        )

        _enforce_verireel_preview_runtime_key_safety(
            record_store=store,
            template_target=_template_target(),
            template_env_map={
                "POSTGRES_PASSWORD": "database-password",
                "BETTER_AUTH_SECRET": "auth-secret",
                "VERIREEL_SECRETS_MASTER_KEY": "master-key",
                "VERIREEL_CRON_SECRET": "cron-secret",
                "VERIREEL_SMOKE_MAINTENANCE_SECRET": "smoke-maintenance-secret",
            },
            request=_refresh_request(),
        )

    def test_preview_refresh_blocks_before_database_bootstrap_without_key_safety_store(
        self,
    ) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://user:pass@db.example/verireel_testing\nEXTERNAL_API_TOKEN=api-token\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command"
            ) as run_command,
        ):
            with self.assertRaisesRegex(click.ClickException, "database storage"):
                execute_verireel_preview_refresh(
                    control_plane_root=Path("."),
                    request=_refresh_request(),
                    record_store=None,
                )

        run_command.assert_not_called()

    def test_preview_refresh_maps_source_of_truth_backend_failure_to_transport(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_control_plane_dokploy_source_of_truth",
                side_effect=click.ClickException(
                    "Could not load tracked Dokploy targets from Launchplane Postgres storage: connection failed"
                ),
            ),
        ):
            with self.assertRaises(VeriReelPreviewRefreshTransportError):
                execute_verireel_preview_refresh(
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                    record_store=None,
                )

    def test_preview_refresh_maps_template_payload_fetch_failure_to_transport(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_control_plane_dokploy_source_of_truth",
                return_value=control_plane_dokploy.DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="verireel",
                            instance="testing",
                            target_type="application",
                            target_id="app-template",
                            target_name="verireel-testing",
                        ),
                    ),
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.fetch_dokploy_target_payload",
                side_effect=click.ClickException(
                    "Dokploy API GET /api/application.one request failed: timed out"
                ),
            ),
        ):
            with self.assertRaises(VeriReelPreviewRefreshTransportError):
                execute_verireel_preview_refresh(
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                    record_store=None,
                )

    def test_preview_refresh_maps_existing_preview_fetch_failure_to_transport(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._fetch_application",
                side_effect=click.ClickException(
                    "Dokploy API GET /api/application.one request failed: timed out"
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command"
            ) as run_command,
        ):
            with self.assertRaises(VeriReelPreviewRefreshTransportError):
                execute_verireel_preview_refresh(
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                    record_store=None,
                )

        run_command.assert_not_called()

    def test_preview_refresh_generates_preview_local_runtime_secrets(self) -> None:
        captured_env: dict[str, str] = {}
        template_master_key = base64.b64encode(b"template-master-key").decode("ascii")

        def capture_environment(**kwargs: object) -> None:
            captured_env.update(
                control_plane_dokploy.parse_dokploy_env_text(str(kwargs["env_text"]))
            )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": (
                            "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n"
                            "BETTER_AUTH_SECRET=template-auth-secret\n"
                            f"VERIREEL_SECRETS_MASTER_KEY={template_master_key}\n"
                            "VERIREEL_CRON_SECRET=template-cron-secret\n"
                            "VERIREEL_SMOKE_MAINTENANCE_SECRET=template-smoke-secret\n"
                        ),
                    },
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value=None,
            ),
            patch("control_plane.workflows.verireel_preview_driver._run_application_command"),
            patch(
                "control_plane.workflows.verireel_preview_driver._ensure_application",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._configure_application",
                side_effect=capture_environment,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._ensure_domain",
                return_value=("domain-preview", ()),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch("control_plane.workflows.verireel_preview_driver.dokploy_api.trigger_deployment"),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.wait_for_target_deployment"
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command_with_retries"
            ),
            patch("control_plane.workflows.verireel_preview_driver._wait_for_preview_health"),
        ):
            result = execute_verireel_preview_refresh(
                control_plane_root=Path(temporary_directory_name),
                request=_refresh_request(),
                record_store=None,
            )

        self.assertEqual(result.refresh_status, "pass")
        self.assertNotEqual(captured_env["BETTER_AUTH_SECRET"], "template-auth-secret")
        self.assertNotEqual(captured_env["VERIREEL_CRON_SECRET"], "template-cron-secret")
        self.assertNotEqual(
            captured_env["VERIREEL_SMOKE_MAINTENANCE_SECRET"],
            "template-smoke-secret",
        )
        self.assertNotEqual(
            captured_env["VERIREEL_SECRETS_MASTER_KEY"],
            template_master_key,
        )
        self.assertEqual(len(base64.b64decode(captured_env["VERIREEL_SECRETS_MASTER_KEY"])), 32)
        self.assertIn("/verireel_preview_pr_71?", captured_env["DATABASE_URL"])

    def test_preview_refresh_reuses_existing_preview_runtime_secrets(self) -> None:
        captured_env: dict[str, str] = {}

        def capture_environment(**kwargs: object) -> None:
            captured_env.update(
                control_plane_dokploy.parse_dokploy_env_text(str(kwargs["env_text"]))
            )

        existing_env = (
            "DATABASE_URL=postgresql://existing:existing-pass@db.example/verireel_preview_pr_71?schema=public\n"
            "BETTER_AUTH_SECRET=existing-auth-secret\n"
            "VERIREEL_SECRETS_MASTER_KEY="
            + base64.b64encode(bytes(range(32))).decode("ascii")
            + "\n"
            "VERIREEL_CRON_SECRET=existing-cron-secret\n"
            "VERIREEL_SMOKE_MAINTENANCE_SECRET=existing-smoke-secret\n"
        )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._find_application_by_name",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._fetch_application",
                return_value={"applicationId": "app-preview", "env": existing_env},
            ),
            patch("control_plane.workflows.verireel_preview_driver._run_application_command"),
            patch(
                "control_plane.workflows.verireel_preview_driver._ensure_application",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._configure_application",
                side_effect=capture_environment,
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._ensure_domain",
                return_value=("", ()),
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch("control_plane.workflows.verireel_preview_driver.dokploy_api.trigger_deployment"),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.wait_for_target_deployment"
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command_with_retries"
            ) as run_command_with_retries,
            patch("control_plane.workflows.verireel_preview_driver._wait_for_preview_health"),
        ):
            result = execute_verireel_preview_refresh(
                control_plane_root=Path(temporary_directory_name),
                request=_refresh_request(),
                record_store=None,
            )

        self.assertEqual(result.refresh_status, "pass")
        self.assertEqual(captured_env["BETTER_AUTH_SECRET"], "existing-auth-secret")
        self.assertEqual(captured_env["VERIREEL_CRON_SECRET"], "existing-cron-secret")
        self.assertEqual(
            captured_env["VERIREEL_SMOKE_MAINTENANCE_SECRET"],
            "existing-smoke-secret",
        )
        self.assertEqual(
            captured_env["VERIREEL_SECRETS_MASTER_KEY"],
            base64.b64encode(bytes(range(32))).decode("ascii"),
        )
        self.assertEqual(
            run_command_with_retries.call_args_list[0].kwargs["command"],
            "./node_modules/.bin/prisma migrate deploy --config prisma.config.ts",
        )
        self.assertEqual(
            run_command_with_retries.call_args_list[1].kwargs["command"],
            "node prisma/seed.mjs",
        )

    def test_preview_destroy_request_requires_pr_scoped_preview_slug(self) -> None:
        with self.assertRaisesRegex(ValueError, "preview_slug to match anchor_pr_number"):
            VeriReelPreviewDestroyRequest.model_validate(
                {
                    "anchor_pr_number": 71,
                    "preview_slug": "pr-72",
                    "destroy_reason": "external_preview_janitor_cleanup_completed",
                }
            )

    def test_run_application_command_uses_shared_schedule_runner(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_preview_driver._upsert_application_schedule",
                return_value="schedule-one",
            ),
            patch(
                "control_plane.workflows.verireel_preview_driver.dokploy_api.run_dokploy_schedule"
            ) as run_schedule,
        ):
            _run_application_command(
                host="https://dokploy.example.com",
                token="secret-token",
                application_id="application-123",
                schedule_name="preview-migrate",
                command="./node_modules/.bin/prisma migrate deploy --config prisma.config.ts",
                timeout_seconds=60,
            )

        run_schedule.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            schedule_id="schedule-one",
            timeout_seconds=60,
        )

    def test_run_application_command_with_retries_retries_transient_provider_failure(self) -> None:
        transient_error = control_plane_dokploy.DokployRequestFailed(
            method="GET",
            path="/api/schedule.list",
            status_code=503,
            detail="provider unavailable",
        )
        with (
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command",
                side_effect=[transient_error, None],
            ) as run_command,
            patch("control_plane.workflows.verireel_preview_driver.time.sleep") as sleep,
        ):
            _run_application_command_with_retries(
                host="https://dokploy.example.com",
                token="secret-token",
                application_id="application-123",
                schedule_name="preview-migrate",
                command="./node_modules/.bin/prisma migrate deploy --config prisma.config.ts",
                timeout_seconds=60,
                attempts=2,
                retry_delay_seconds=1.5,
            )

        self.assertEqual(run_command.call_count, 2)
        sleep.assert_called_once_with(1.5)

    def test_run_application_command_with_retries_does_not_repeat_deterministic_failure(
        self,
    ) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_preview_driver._run_application_command",
                side_effect=click.ClickException("still failing"),
            ) as run_command,
            patch("control_plane.workflows.verireel_preview_driver.time.sleep") as sleep,
        ):
            with self.assertRaises(click.ClickException):
                _run_application_command_with_retries(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    application_id="application-123",
                    schedule_name="preview-seed",
                    command="node prisma/seed.mjs",
                    timeout_seconds=60,
                    attempts=2,
                    retry_delay_seconds=2.0,
                )

        self.assertEqual(run_command.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
