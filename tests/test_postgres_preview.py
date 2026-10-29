import base64
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import click
from control_plane.workflows.preview_resource_destroy import PREVIEW_CLEANUP_SAFETY_UNKNOWN

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import DokployTargetDefinition
from control_plane.workflows.postgres_preview import PostgresPreviewDestroyRequest
from control_plane.workflows.postgres_preview import PostgresPreviewRefreshRequest
from control_plane.workflows.postgres_preview import PostgresPreviewRefreshConfigError
from control_plane.workflows.postgres_preview import PostgresPreviewRefreshTransportError
from control_plane.workflows.postgres_preview import _preview_application_name
from control_plane.workflows.postgres_preview import _preview_database_identifiers
from control_plane.workflows.postgres_preview import _build_preview_runtime_identity
from control_plane.workflows.postgres_preview import _build_preview_database_command
from control_plane.workflows.postgres_preview import _build_admin_database_url
from control_plane.workflows.postgres_preview import _ensure_application
from control_plane.workflows.postgres_preview import _resolve_preview_secret
from control_plane.workflows.postgres_preview import _resolve_preview_url
from control_plane.workflows.postgres_preview import _run_application_command
from control_plane.workflows.postgres_preview import _run_application_command_with_retries
from control_plane.workflows.postgres_preview import _wait_for_preview_health
from control_plane.workflows.postgres_preview import _template_runtime_secret_keys
from control_plane.workflows.postgres_preview import execute_postgres_preview_destroy
from control_plane.workflows.postgres_preview import execute_postgres_preview_refresh
from control_plane.workflows.preview_resource_destroy import PreviewResourceDestroyResult
from tests.test_generic_web_preview_extensions import _driver_profile


def _preview_store(profile: object = None) -> object:
    return SimpleNamespace(list_product_profile_records=lambda: (profile or _driver_profile(),))


def _template_target() -> DokployTargetDefinition:
    return DokployTargetDefinition(
        context="verireel",
        instance="testing",
        target_type="application",
        target_id="app-template",
        target_name="verireel-testing",
    )


def _refresh_request() -> PostgresPreviewRefreshRequest:
    return PostgresPreviewRefreshRequest.model_validate(
        {
            "context": "verireel-testing",
            "anchor_repo": "verireel",
            "anchor_pr_number": 71,
            "anchor_pr_url": "https://github.com/every/verireel/pull/71",
            "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
            "preview_slug": "pr-71",
            "preview_url": "https://pr-71.ver-preview.shinycomputers.com",
            "image_reference": "ghcr.io/every/verireel-app:pr-71-sha-6b3c9d7",
        }
    )


class PostgresPreviewDriverTests(unittest.TestCase):
    def test_long_database_names_remain_distinct_and_within_postgres_limit(self) -> None:
        prefix = "sample_product_preview_database_"
        first = _preview_database_identifiers("change-for-a-very-long-preview-one-41", prefix)
        second = _preview_database_identifiers("change-for-a-very-long-preview-one-42", prefix)
        self.assertNotEqual(first, second)
        self.assertEqual(
            first, _preview_database_identifiers("change-for-a-very-long-preview-one-41", prefix)
        )
        self.assertTrue(all(len(name.encode()) <= 63 for name in (*first, *second)))

    def test_conflicting_or_unknown_product_namespaces_refuse_before_provider_calls(self) -> None:
        profile = _driver_profile()
        other = profile.model_copy(update={"product": "other"})
        for store in (
            object(),
            SimpleNamespace(list_product_profile_records=lambda: (profile, other)),
        ):
            with (
                self.subTest(store=store),
                patch(
                    "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config"
                ) as provider,
            ):
                with self.assertRaises(PostgresPreviewRefreshConfigError):
                    execute_postgres_preview_refresh(
                        record_store=store,
                        profile=profile,
                        control_plane_root=Path("."),
                        request=_refresh_request(),
                    )
                with self.assertRaises(PostgresPreviewRefreshConfigError):
                    execute_postgres_preview_destroy(
                        record_store=store,
                        profile=profile,
                        control_plane_root=Path("."),
                        request=PostgresPreviewDestroyRequest(
                            context=profile.preview.context,
                            anchor_repo="verireel",
                            anchor_pr_number=71,
                            preview_slug="pr-71",
                            destroy_reason="closed",
                        ),
                    )
                provider.assert_not_called()

    def test_retired_transport_is_storage_compatibility_only(self) -> None:
        from control_plane.contracts.product_profile_record import ProductPreviewProfile
        from control_plane.product_generic_web_transition import GenericWebPreviewDeclaration

        legacy = {
            **_driver_profile().preview.model_dump(mode="json"),
            "transport_extension": "verireel-postgres",
        }
        with self.assertRaises(ValueError):
            ProductPreviewProfile.model_validate(legacy)
        with self.assertRaises(ValueError):
            GenericWebPreviewDeclaration.model_validate(
                {
                    "data_transport_mode": "driver",
                    "transport_extension": legacy["transport_extension"],
                }
            )
        restored = ProductPreviewProfile.model_validate(
            legacy, context={"stored_product_profile": True}
        )
        self.assertEqual(restored, _driver_profile().preview)

    def test_generated_secrets_cannot_replace_managed_preview_keys(self) -> None:
        from control_plane.contracts.product_profile_record import ProductPreviewProfile

        payload = _driver_profile().preview.model_dump(mode="json")
        for key in (
            "DATABASE_URL",
            "GH_TOKEN",
            "DOKPLOY_TOKEN",
            "LAUNCHPLANE_PRODUCT",
            "invalid key",
            *payload["preview_url_env_keys"],
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ProductPreviewProfile.model_validate(
                    {**payload, "generated_secret_env": {key: "urlsafe"}}
                )

    def test_production_template_refuses_before_database_or_application_effects(self) -> None:
        profile = _driver_profile()
        profile = profile.model_copy(
            update={
                "preview": profile.preview.model_copy(update={"template_instance": "prod"}),
                "lanes": (profile.lanes[0].model_copy(update={"instance": "prod"}),),
            }
        )
        with (
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("provider", "fixture"),
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request"
            ) as provider,
            self.assertRaises(PostgresPreviewRefreshConfigError),
        ):
            execute_postgres_preview_refresh(
                record_store=_preview_store(),
                control_plane_root=Path("."),
                request=_refresh_request(),
                profile=profile,
            )
        provider.assert_not_called()

    def setUp(self) -> None:
        self.enterContext(
            patch("control_plane.dokploy.memory_capacity.require_preview_host_memory_capacity")
        )
        self.lifecycle = MagicMock()
        self.lifecycle_factory = self.enterContext(
            patch(
                "control_plane.workflows.postgres_preview.preview_application_lifecycle",
                return_value=self.lifecycle,
            )
        )

    def test_shared_database_or_role_refuses_before_refresh_or_destroy(self) -> None:
        database, role = _preview_database_identifiers("pr-71", "verireel_preview_")
        template_url = "postgresql://admin:password@db:5432/testing"
        for database_url, template, app_present in (
            (f"postgresql://{role}:password@db:5432/production", template_url, True),
            (f"postgresql://production:password@db:5432/{database}", template_url, True),
            (f"postgresql://{role}:password@other-host:5432/{database}", template_url, True),
            (f"postgresql://{role}:password@db:5433/{database}", template_url, True),
            ("", f"postgresql://admin:password@db:5432/{database}", False),
            ("", f"postgresql://{role}:password@db:5432/testing", False),
            ("", f"postgresql://admin:password@db:5432/{database}", True),
        ):
            for operation in ("refresh", "destroy"):
                with (
                    self.subTest(
                        database_url=database_url,
                        template=template,
                        app_present=app_present,
                        operation=operation,
                    ),
                    TemporaryDirectory() as directory,
                    patch(
                        "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                        return_value=("https://dokploy.example.invalid", "fixture-token"),
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._template_application_payload",
                        return_value=(
                            _template_target(),
                            {"env": f"DATABASE_URL={template}"},
                        ),
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._resolve_preview_url_for_destroy",
                        return_value="https://preview.example.invalid",
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._find_application_by_name",
                        return_value={"applicationId": "app-preview"} if app_present else None,
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._fetch_application",
                        return_value={"env": f"DATABASE_URL={database_url}"},
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview.destroy_dokploy_preview_resource"
                    ) as destroy,
                    patch(
                        "control_plane.workflows.postgres_preview._run_application_command"
                    ) as bootstrap,
                    patch(
                        "control_plane.workflows.postgres_preview._run_application_command_with_retries"
                    ) as drop,
                    patch(
                        "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request",
                        side_effect=AssertionError("Unsafe database reached provider effects"),
                    ),
                ):
                    with self.assertRaises(PostgresPreviewRefreshConfigError):
                        if operation == "destroy":
                            execute_postgres_preview_destroy(
                                record_store=_preview_store(),
                                profile=_driver_profile(),
                                control_plane_root=Path(directory),
                                request=PostgresPreviewDestroyRequest(
                                    context="verireel-testing",
                                    anchor_repo="verireel",
                                    anchor_pr_number=71,
                                    preview_slug="pr-71",
                                    destroy_reason="pull request closed",
                                ),
                            )
                        else:
                            execute_postgres_preview_refresh(
                                record_store=_preview_store(),
                                profile=_driver_profile(),
                                control_plane_root=Path(directory),
                                request=_refresh_request(),
                            )
                    destroy.assert_not_called()
                    bootstrap.assert_not_called()
                    drop.assert_not_called()

    def test_preview_runtime_identity_uses_launchplane_preview_binding(self) -> None:
        identity = _build_preview_runtime_identity(
            product="verireel",
            request=_refresh_request(),
            preview_id="preview-verireel-testing-verireel-pr-71",
            preview_generation_id=("preview-verireel-testing-verireel-pr-71-generation-0003"),
        )

        self.assertEqual(identity.preview_id, "preview-verireel-testing-verireel-pr-71")
        self.assertEqual(
            identity.preview_generation_id,
            "preview-verireel-testing-verireel-pr-71-generation-0003",
        )

    def test_destroy_removes_application_and_its_attached_schedules(
        self,
    ) -> None:
        request = PostgresPreviewDestroyRequest.model_validate(
            {
                "context": "verireel-testing",
                "anchor_repo": "verireel",
                "anchor_pr_number": 71,
                "destroy_reason": "pull request closed",
                "preview_slug": "pr-71",
            }
        )
        cleanup_order: list[str] = []
        database, role = _preview_database_identifiers(request.preview_slug, "verireel_preview_")
        template_database_url = "postgresql://admin:password@db:5432/verireel"

        def _destroy_application(**kwargs: object) -> PreviewResourceDestroyResult:
            cleanup_order.append("application")
            return PreviewResourceDestroyResult(status="pass")

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {"env": f"DATABASE_URL={template_database_url}"},
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._resolve_preview_url_for_destroy",
                return_value="https://pr-71.ver-preview.shinycomputers.com",
            ),
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.postgres_preview._fetch_application",
                return_value={
                    "env": f"DATABASE_URL=postgresql://{role}:password@db:5432/{database}"
                },
            ),
            patch(
                "control_plane.workflows.postgres_preview.destroy_dokploy_preview_resource",
                side_effect=_destroy_application,
            ) as destroy_resource,
            patch(
                "control_plane.workflows.postgres_preview._run_application_command_with_retries"
            ) as drop,
            patch(
                "control_plane.workflows.postgres_preview._build_preview_database_command",
                wraps=_build_preview_database_command,
            ) as build_command,
        ):
            result = execute_postgres_preview_destroy(
                record_store=_preview_store(),
                profile=_driver_profile(),
                control_plane_root=Path(temporary_directory_name),
                request=request,
            )
            self.assertEqual(cleanup_order, ["application"])
            destroy_resource.assert_called_once()
            self.assertEqual(result.destroy_status, "pass")
            self.assertEqual(result.error_message, "")

            with patch(
                "control_plane.workflows.postgres_preview.destroy_dokploy_preview_resource",
                return_value=PreviewResourceDestroyResult(
                    status="fail",
                    cleanup_errors=(f"{PREVIEW_CLEANUP_SAFETY_UNKNOWN}: shared target",),
                ),
            ):
                drop.reset_mock()
                refused = execute_postgres_preview_destroy(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )
                self.assertEqual(refused.destroy_status, "fail")
                drop.assert_not_called()

            for application in (None, {"applicationId": "app-preview"}):
                with (
                    self.subTest(application=application),
                    patch(
                        "control_plane.workflows.postgres_preview._find_application_by_name",
                        return_value=application,
                    ),
                    patch(
                        "control_plane.workflows.postgres_preview._fetch_application",
                        return_value={"env": ""},
                    ),
                ):
                    drop.reset_mock()
                    build_command.reset_mock()
                    retried = execute_postgres_preview_destroy(
                        record_store=_preview_store(),
                        profile=_driver_profile(),
                        control_plane_root=Path(temporary_directory_name),
                        request=request,
                    )
                    self.assertEqual(retried.destroy_status, "pass")
                    drop.assert_called_once()
                    build_command.assert_called_once_with(
                        action="drop",
                        admin_database_url=_build_admin_database_url(template_database_url),
                        database_name=database,
                        role_name=role,
                    )

    def test_destroy_refuses_incomplete_inventory_before_any_cleanup(self) -> None:
        request = PostgresPreviewDestroyRequest(
            context="verireel-testing",
            anchor_repo="verireel",
            anchor_pr_number=71,
            destroy_reason="pull request closed",
            preview_slug="pr-71",
        )
        inventories: tuple[object, ...] = (
            [
                {
                    "environments": [
                        {
                            "applications": [
                                {"name": _preview_application_name("pr-71", "ver-preview", "-app")}
                            ]
                        }
                    ]
                }
            ],
            {},
            [{"environments": [{"applications": None}]}],
            [
                {
                    "environments": [
                        {
                            "applications": [
                                {
                                    "name": _preview_application_name(
                                        "pr-71", "ver-preview", "-app"
                                    ),
                                    "applicationId": "preview",
                                },
                                None,
                            ]
                        }
                    ]
                }
            ],
        )
        for inventory in inventories:
            with (
                self.subTest(inventory=inventory),
                TemporaryDirectory() as directory,
                patch(
                    "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "fixture-token"),
                ),
                patch(
                    "control_plane.workflows.postgres_preview._template_application_payload",
                    return_value=(
                        _template_target(),
                        {"env": "DATABASE_URL=postgresql://admin:password@db:5432/verireel"},
                    ),
                ),
                patch(
                    "control_plane.workflows.postgres_preview._resolve_preview_url_for_destroy",
                    return_value="https://preview.example.invalid",
                ),
                patch(
                    "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request",
                    return_value=inventory,
                ),
                patch(
                    "control_plane.workflows.postgres_preview.destroy_dokploy_preview_resource"
                ) as destroy,
                patch(
                    "control_plane.workflows.postgres_preview._run_application_command_with_retries"
                ) as database_cleanup,
            ):
                result = execute_postgres_preview_destroy(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(directory),
                    request=request,
                )
            self.assertEqual(result.destroy_status, "fail")
            self.assertEqual(result.application_id, "")
            self.assertTrue(result.error_message)
            destroy.assert_not_called()
            database_cleanup.assert_not_called()

    def test_ensure_application_uses_default_server_when_template_omits_server_id(self) -> None:
        requests: list[dict[str, object]] = []

        def _create_application(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {"applicationId": "app-preview"}

        with (
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request",
                side_effect=_create_application,
            ),
            patch(
                "control_plane.workflows.postgres_preview._fetch_application",
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
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request",
                side_effect=_create_application,
            ),
            patch(
                "control_plane.workflows.postgres_preview._fetch_application",
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

    def test_database_commands_quote_identifiers_and_passwords(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "node_modules" / "pg"
            package.mkdir(parents=True)
            (root / "package.json").write_text("{}")
            (package / "index.js").write_text(
                "exports.Client = class { async connect() {} async end() {} "
                "async query(sql, params = []) { console.log(JSON.stringify([sql, params])); "
                "return { rowCount: 0 }; } };"
            )
            database, role, password = 'preview_"database', 'preview_"role', "pass'word"
            for action in ("ensure", "drop"):
                with self.subTest(action=action):
                    command = _build_preview_database_command(
                        action=action,
                        admin_database_url="postgresql://fixture:fixture@db/postgres",
                        database_name=database,
                        role_name=role,
                        password=password,
                    )
                    result = subprocess.run(
                        ["sh", "-c", command], cwd=root, capture_output=True, text=True, check=True
                    )
                    queries = [json.loads(line) for line in result.stdout.splitlines()]
                    database_identifier = '"' + database.replace('"', '""') + '"'
                    role_identifier = '"' + role.replace('"', '""') + '"'
                    statements = [query[0] for query in queries]
                    if action == "ensure":
                        self.assertTrue(any(role in params for _, params in queries))
                        self.assertTrue(
                            any(
                                statement.startswith("CREATE ")
                                and database_identifier in statement
                                and role_identifier in statement
                                for statement in statements
                            )
                        )
                        self.assertTrue(
                            any("'pass''word'" in statement for statement in statements)
                        )
                    else:
                        for identifier in (database_identifier, role_identifier):
                            self.assertTrue(
                                any(
                                    statement.startswith("DROP ") and identifier in statement
                                    for statement in statements
                                )
                            )

    def test_preview_refresh_request_requires_pr_scoped_preview_slug(self) -> None:
        with self.assertRaisesRegex(ValueError, "preview_slug to match anchor_pr_number"):
            PostgresPreviewRefreshRequest.model_validate(
                {
                    "context": "verireel-testing",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 71,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/71",
                    "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                    "preview_slug": "pr-72",
                    "preview_url": "https://pr-71.ver-preview.shinycomputers.com",
                    "image_reference": "ghcr.io/every/verireel-app:pr-71-sha-6b3c9d7",
                }
            )

    def test_preview_refresh_can_derive_preview_url_from_runtime_records(self) -> None:
        request = PostgresPreviewRefreshRequest.model_validate(
            {
                "context": "verireel-testing",
                "anchor_repo": "verireel",
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
                "control_plane.workflows.postgres_preview.control_plane_runtime_environments.resolve_runtime_context_values",
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

    def test_template_runtime_secret_keys_skip_rewritten_database_url(self) -> None:
        self.assertEqual(
            _template_runtime_secret_keys(
                {
                    "DATABASE_URL": "postgresql://user:pass@db.example/verireel_testing",
                    "BETTER_AUTH_SECRET": "auth-secret",
                    "VERIREEL_SECRETS_MASTER_KEY": "master-key",
                    "VERIREEL_CRON_SECRET": "cron-secret",
                    "VERIREEL_SMOKE_MAINTENANCE_SECRET": "smoke-secret",
                    "NEXT_PUBLIC_SITE_URL": "https://testing.example",
                },
                frozenset({"DATABASE_URL", *_driver_profile().preview.generated_secret_env}),
            ),
            (),
        )

    def test_template_credentials_are_found_by_name_or_embedded_password(self) -> None:
        self.assertEqual(
            _template_runtime_secret_keys(
                {
                    "POSTGRES_PASSWORD": "pg",
                    "PGPASSWORD": "pg",
                    "STRIPE_APIKEY": "sk",
                    "REDIS_URL": "redis://default:redis-pass@redis.example:6379",
                    "S3_ENDPOINT": "https://s3.example",
                    "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test",
                    "APP_MODE": "testing",
                }
            ),
            ("POSTGRES_PASSWORD", "PGPASSWORD", "STRIPE_APIKEY", "REDIS_URL"),
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

    def test_preview_refresh_refuses_template_platform_credential(self) -> None:
        with (
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://user:pass@db.example/verireel_testing\nGITHUB_TOKEN=template-token-value\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._run_application_command"
            ) as run_command,
        ):
            with self.assertRaises(PostgresPreviewRefreshConfigError) as refusal:
                execute_postgres_preview_refresh(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path("."),
                    request=_refresh_request(),
                )

        self.assertIn("GITHUB_TOKEN", refusal.exception.message)
        self.assertIn("template application env", refusal.exception.message)
        self.assertNotIn("template-token-value", refusal.exception.message)
        run_command.assert_not_called()

    def test_preview_refresh_maps_source_of_truth_backend_failure_to_transport(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_control_plane_dokploy_source_of_truth",
                side_effect=click.ClickException(
                    "Could not load tracked Dokploy targets from Launchplane Postgres storage: connection failed"
                ),
            ),
        ):
            with self.assertRaises(PostgresPreviewRefreshTransportError):
                execute_postgres_preview_refresh(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                )

    def test_preview_refresh_maps_template_payload_fetch_failure_to_transport(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_control_plane_dokploy_source_of_truth",
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
                "control_plane.workflows.postgres_preview.dokploy_api.fetch_dokploy_target_payload",
                side_effect=click.ClickException(
                    "Dokploy API GET /api/application.one request failed: timed out"
                ),
            ),
        ):
            with self.assertRaises(PostgresPreviewRefreshTransportError):
                execute_postgres_preview_refresh(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                )

    def test_preview_refresh_maps_existing_preview_fetch_failure_to_transport(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.postgres_preview._fetch_application",
                side_effect=click.ClickException(
                    "Dokploy API GET /api/application.one request failed: timed out"
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._run_application_command"
            ) as run_command,
        ):
            with self.assertRaises(PostgresPreviewRefreshTransportError):
                execute_postgres_preview_refresh(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(temporary_directory_name),
                    request=_refresh_request(),
                )

        run_command.assert_not_called()

    def test_refresh_maps_incomplete_inventory_to_transport_before_writes(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "fixture-token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {"env": "DATABASE_URL=postgresql://admin:password@db:5432/verireel"},
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._resolve_preview_url",
                return_value="https://preview.example.invalid",
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.dokploy_request",
                return_value=[{"environments": None}],
            ),
            patch("control_plane.workflows.postgres_preview._run_application_command") as database,
            patch("control_plane.workflows.postgres_preview._ensure_application") as application,
        ):
            with self.assertRaises(PostgresPreviewRefreshTransportError):
                execute_postgres_preview_refresh(
                    record_store=_preview_store(),
                    profile=_driver_profile(),
                    control_plane_root=Path(directory),
                    request=_refresh_request(),
                )
        database.assert_not_called()
        application.assert_not_called()

    def test_preview_refresh_generates_its_own_secrets_and_copies_none_from_testing(
        self,
    ) -> None:
        from tests.test_generic_web_preview_extensions import _driver_profile

        profile = _driver_profile(driver_id="generic-web", declared_transport=True)
        profile = profile.model_copy(
            update={
                "product": "sample-product",
                "runtime_port": 8080,
                "health_path": "/ready",
                "lanes": (profile.lanes[0].model_copy(update={"context": "sample-template"}),),
                "preview": profile.preview.model_copy(
                    update={
                        "app_name_prefix": "sample-preview",
                        "app_name_suffix": "-app",
                        "transport_database_prefix": "sample_data_",
                        "generated_secret_env": {
                            "SESSION_SECRET": "urlsafe",
                            "ENCRYPTION_KEY": "base64",
                        },
                        "preview_url_env_keys": ("PUBLIC_APP_URL",),
                        "preview_domain_env_keys": ("PUBLIC_DOMAIN",),
                    }
                ),
            }
        )
        captured_env: dict[str, str] = {}
        template_master_key = base64.b64encode(b"template-master-key").decode("ascii")

        def capture_environment(**kwargs: object) -> None:
            captured_env.update(
                control_plane_dokploy.parse_dokploy_env_text(str(kwargs["env_text"]))
            )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": (
                            "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n"
                            "SESSION_SECRET=template-auth-secret\n"
                            f"ENCRYPTION_KEY={template_master_key}\n"
                            "POSTGRES_PASSWORD=template-postgres-password\n"
                            "NEXT_PUBLIC_SUPPORT_EMAIL=support@example.com\n"
                        ),
                    },
                ),
            ) as template_lookup,
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value=None,
            ),
            patch("control_plane.workflows.postgres_preview._run_application_command"),
            patch(
                "control_plane.workflows.postgres_preview._ensure_application",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.postgres_preview._configure_application",
                side_effect=capture_environment,
            ),
            patch(
                "control_plane.workflows.postgres_preview._ensure_domain",
                return_value=("domain-preview", ()),
            ) as domain,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch("control_plane.workflows.postgres_preview.dokploy_api.trigger_deployment"),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.wait_for_target_deployment"
            ),
            patch("control_plane.workflows.postgres_preview._run_application_command_with_retries"),
            patch(
                "control_plane.workflows.postgres_preview._wait_for_preview_health",
                side_effect=lambda **kwargs: kwargs["expected_runtime_identity"],
            ) as health,
        ):
            result = execute_postgres_preview_refresh(
                record_store=_preview_store(profile),
                profile=profile,
                control_plane_root=Path(temporary_directory_name),
                request=_refresh_request(),
            )

        self.assertEqual(result.refresh_status, "pass")
        self.assertEqual(result.application_id, "app-preview")
        self.assertNotEqual(captured_env["SESSION_SECRET"], "template-auth-secret")
        self.assertNotEqual(
            captured_env["ENCRYPTION_KEY"],
            template_master_key,
        )
        self.assertEqual(len(base64.b64decode(captured_env["ENCRYPTION_KEY"])), 32)
        self.assertIn(
            f"/{profile.preview.transport_database_prefix}pr_71?", captured_env["DATABASE_URL"]
        )
        self.assertEqual(result.application_name, f"{profile.preview.app_name_prefix}-pr-71-app")
        self.assertEqual(
            template_lookup.call_args.kwargs["template_context"], profile.lanes[0].context
        )
        self.assertEqual(
            template_lookup.call_args.kwargs["template_instance"], profile.lanes[0].instance
        )
        self.assertIsNotNone(result.runtime_identity)
        assert result.runtime_identity is not None
        self.assertEqual(result.runtime_identity.product, profile.product)
        self.assertNotIn("POSTGRES_PASSWORD", captured_env)
        self.assertNotIn("template-postgres-password", "\n".join(captured_env.values()))
        self.assertEqual(captured_env["NEXT_PUBLIC_SUPPORT_EMAIL"], "support@example.com")
        self.assertEqual(domain.call_args.kwargs["runtime_port"], profile.runtime_port)
        self.assertEqual(health.call_args.kwargs["health_path"], profile.health_path)
        self.assertEqual(captured_env["PUBLIC_APP_URL"], _refresh_request().preview_url)
        self.assertFalse(any(key.startswith("VERIREEL_") for key in captured_env))
        self.lifecycle.complete.assert_called_once()
        self.assertEqual(
            self.lifecycle_factory.call_args.kwargs["extensions"],
            profile.preview.generic_web_extensions,
        )

    def test_new_preview_early_failure_rolls_back_database_without_unbound_application_id(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.postgres_preview._run_application_command",
                side_effect=[click.ClickException("bootstrap failed"), None],
            ) as run_command,
        ):
            result = execute_postgres_preview_refresh(
                record_store=_preview_store(),
                profile=_driver_profile(),
                control_plane_root=Path(temporary_directory_name),
                request=_refresh_request(),
            )

        self.assertEqual(result.refresh_status, "fail")
        self.assertIn("bootstrap failed", result.error_message)
        self.assertEqual(run_command.call_count, 2)

    def test_preview_refresh_reuses_existing_preview_runtime_secrets(self) -> None:
        self.enterContext(
            patch(
                "control_plane.workflows.postgres_preview.generate_deployment_record_id",
                return_value="deployment-preview-secret-reuse",
            )
        )
        captured_env: dict[str, str] = {}

        def capture_environment(**kwargs: object) -> None:
            captured_env.update(
                control_plane_dokploy.parse_dokploy_env_text(str(kwargs["env_text"]))
            )

        database, role = _preview_database_identifiers("pr-71", "verireel_preview_")
        existing_env = (
            f"DATABASE_URL=postgresql://{role}:existing-pass@db.example/{database}?schema=public\n"
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
                "control_plane.workflows.postgres_preview.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.postgres_preview._template_application_payload",
                return_value=(
                    _template_target(),
                    {
                        "applicationId": "app-template",
                        "env": "DATABASE_URL=postgresql://template:template-pass@db.example/verireel_testing\n",
                    },
                ),
            ),
            patch(
                "control_plane.workflows.postgres_preview._find_application_by_name",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.postgres_preview._fetch_application",
                return_value={"applicationId": "app-preview", "env": existing_env},
            ),
            patch("control_plane.workflows.postgres_preview._run_application_command"),
            patch(
                "control_plane.workflows.postgres_preview._ensure_application",
                return_value={"applicationId": "app-preview"},
            ),
            patch(
                "control_plane.workflows.postgres_preview._configure_application",
                side_effect=capture_environment,
            ),
            patch(
                "control_plane.workflows.postgres_preview._ensure_domain",
                return_value=("", ()),
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.latest_deployment_for_target",
                return_value=None,
            ),
            patch("control_plane.workflows.postgres_preview.dokploy_api.trigger_deployment"),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.wait_for_target_deployment"
            ),
            patch("control_plane.workflows.postgres_preview._run_application_command_with_retries"),
            patch(
                "control_plane.workflows.postgres_preview._wait_for_preview_health",
                side_effect=lambda **kwargs: kwargs["expected_runtime_identity"],
            ),
        ):
            result = execute_postgres_preview_refresh(
                record_store=_preview_store(),
                profile=_driver_profile(),
                control_plane_root=Path(temporary_directory_name),
                request=_refresh_request(),
            )

        self.assertEqual(result.refresh_status, "pass")
        self.lifecycle.prepare.assert_called_once()
        self.lifecycle.complete.assert_called_once()
        self.assertEqual(
            result.runtime_identity,
            _build_preview_runtime_identity(product="verireel", request=_refresh_request()),
        )
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
        self.lifecycle.maintain.assert_called_once()

    def test_wait_for_preview_health_returns_matching_runtime_identity(self) -> None:
        expected = _build_preview_runtime_identity(product="verireel", request=_refresh_request())
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "ok": True,
                "runtime_identity": expected.model_dump(mode="json"),
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with patch("control_plane.workflows.postgres_preview.urlopen", return_value=response):
            observed = _wait_for_preview_health(
                preview_url="https://pr-71.preview.example.test",
                timeout_seconds=30,
                expected_runtime_identity=expected,
            )

        self.assertEqual(observed, expected)

    def test_wait_for_preview_health_rejects_runtime_identity_mismatch(self) -> None:
        expected = _build_preview_runtime_identity(product="verireel", request=_refresh_request())
        mismatched = expected.model_copy(update={"source_git_ref": "a" * 40})
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "ok": True,
                "runtime_identity": mismatched.model_dump(mode="json"),
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with (
            patch("control_plane.workflows.postgres_preview.urlopen", return_value=response),
            patch(
                "control_plane.workflows.postgres_preview.time.monotonic",
                side_effect=(0.0, 0.0, 0.0, 2.0),
            ),
            patch("control_plane.workflows.postgres_preview.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(click.ClickException, "source_git_ref"):
                _wait_for_preview_health(
                    preview_url="https://pr-71.preview.example.test",
                    timeout_seconds=1,
                    expected_runtime_identity=expected,
                )

        sleep.assert_not_called()

    def test_wait_for_preview_health_fails_immediately_on_hard_identity_mismatch(
        self,
    ) -> None:
        expected = _build_preview_runtime_identity(product="verireel", request=_refresh_request())
        mismatched = expected.model_copy(update={"product": "different-product"})
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "ok": True,
                "runtime_identity": mismatched.model_dump(mode="json"),
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        with (
            patch("control_plane.workflows.postgres_preview.urlopen", return_value=response),
            patch(
                "control_plane.workflows.postgres_preview.time.monotonic",
                side_effect=(0.0, 0.0, 0.0),
            ),
            patch("control_plane.workflows.postgres_preview.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(click.ClickException, "product"):
                _wait_for_preview_health(
                    preview_url="https://pr-71.preview.example.test",
                    timeout_seconds=30,
                    expected_runtime_identity=expected,
                )

        sleep.assert_not_called()

    def test_preview_destroy_request_requires_pr_scoped_preview_slug(self) -> None:
        with self.assertRaisesRegex(ValueError, "preview_slug to match anchor_pr_number"):
            PostgresPreviewDestroyRequest.model_validate(
                {
                    "context": "verireel-testing",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 71,
                    "preview_slug": "pr-72",
                    "destroy_reason": "external_preview_janitor_cleanup_completed",
                }
            )

    def test_run_application_command_uses_shared_schedule_runner(self) -> None:
        with (
            patch(
                "control_plane.workflows.postgres_preview._upsert_application_schedule",
                return_value="schedule-one",
            ),
            patch(
                "control_plane.workflows.postgres_preview.dokploy_api.run_dokploy_schedule"
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
        with patch.object(dokploy_api, "dokploy_request", side_effect=transient_error):
            with self.assertRaises(dokploy_api.DokployScheduleExecutionFailed) as caught:
                dokploy_api.read_dokploy_schedule_baseline(
                    host="https://provider.invalid", token="fixture", schedule_id="fixture"
                )
        for error in (transient_error, caught.exception):
            with (
                patch(
                    "control_plane.workflows.postgres_preview._run_application_command",
                    side_effect=[error, None],
                ) as run_command,
                patch("control_plane.workflows.postgres_preview.time.sleep") as sleep,
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
                "control_plane.workflows.postgres_preview._run_application_command",
                side_effect=click.ClickException("still failing"),
            ) as run_command,
            patch("control_plane.workflows.postgres_preview.time.sleep") as sleep,
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
