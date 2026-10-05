from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import click
from urllib.error import HTTPError
from email.message import Message
from types import SimpleNamespace
from control_plane.runtime_environments import (
    RuntimeEnvironmentDefinition,
    RuntimeEnvironmentContextDefinition,
    build_runtime_environment_definition_from_records,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.github_app_identity import (
    resolve_advisory_github_app_identity,
    ADVISORY_GITHUB_APP_ID_ENV_KEY,
)
from control_plane.merge_train_github import _owner_review_advisory_app_id
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.contracts.merge_train_policy import MergeTrainGitHubTokenSource
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppInstallationToken,
    mint_delivery_installation_token,
)
from control_plane.launchplane_github_delivery import (
    DELIVERY_GITHUB_APP_ID_KEY,
    DELIVERY_GITHUB_APP_INTEGRATION_KEY,
    resolve_delivery_github_app_identity,
    resolve_delivery_github_token,
    delivery_github_credentials_ready,
    DeliveryGitHubTokenUnavailable,
)
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.launchplane import resolve_launchplane_github_token
from tests.test_product_repository_identity import _inventory


class DeliveryGitHubTokenTests(unittest.TestCase):
    key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = FilesystemRecordStore(self.root / "state")
        self.store.write_repository_inventory_record(_inventory(repository="example/site"))
        self.requested: list[dict[str, object]] = []
        self.installation_permissions = {
            "metadata": "read",
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "actions": "write",
            "checks": "read",
            "statuses": "read",
            "administration": "read",
        }
        self.token_repository = "example/site"
        self.extra_token_permissions: dict[str, str] = {}
        self.revoked = False
        self.provider_error: Exception | None = None
        self.now = datetime.now(UTC)
        self.identity = GitHubAppIdentity(app_id=76, private_key=self.key)
        database = patch(
            "control_plane.launchplane_github_delivery.resolve_database_url", return_value=None
        )
        database.start()
        self.addCleanup(database.stop)

    def provider(self, **kwargs: object) -> object:
        path = kwargs["path"]
        if path == "/app":
            if self.provider_error is not None:
                raise click.ClickException("Provider unavailable") from self.provider_error
            return {"id": self.identity.app_id}
        if path == "/repos/example/site/installation":
            return {
                "id": 12,
                "app_id": self.identity.app_id,
                "permissions": self.installation_permissions,
            }
        if path == "/installation/token":
            self.revoked = True
            return None
        if path == "/app/installations/12/access_tokens":
            body = kwargs["body"]
            assert isinstance(body, dict)
            self.requested.append(body)
            permissions = body["permissions"]
            assert isinstance(permissions, dict)
            return {
                "token": "installation-token",
                "expires_at": (self.now + timedelta(hours=1)).isoformat(),
                "permissions": {"metadata": "read", **permissions, **self.extra_token_permissions},
                "repositories": [{"id": 7001, "full_name": self.token_repository}],
            }
        raise AssertionError(f"Unexpected provider call {path}")

    def resolve(
        self, purpose: str, *, repository: str = "example/site", retry_provider_errors: bool = False
    ) -> str:
        def mint(
            *,
            identity: GitHubAppIdentity,
            repository: str,
            repository_id: str,
            permissions: Mapping[str, str],
        ) -> GitHubAppInstallationToken:
            return mint_delivery_installation_token(
                identity=identity,
                repository=repository,
                repository_id=repository_id,
                permissions=permissions,
                api_request=self.provider,
                now=self.now,
            )

        with (
            patch(
                "control_plane.launchplane_github_delivery.resolve_delivery_github_app_identity",
                return_value=self.identity,
            ),
            patch(
                "control_plane.launchplane_github_delivery.mint_delivery_installation_token",
                side_effect=mint,
            ),
        ):
            return resolve_delivery_github_token(
                control_plane_root=self.root,
                context_name="site",
                repository=repository,
                purpose=purpose,
                retry_provider_errors=retry_provider_errors,
            )

    def test_each_operation_mints_only_its_permissions_for_the_inventory_repository(self) -> None:
        for purpose, permissions in (
            ("repository_read", {"contents": "read", "pull_requests": "read"}),
            ("pull_request_feedback", {"contents": "read", "pull_requests": "write"}),
            (
                "source_issue_feedback",
                {"contents": "read", "pull_requests": "write", "issues": "write"},
            ),
            ("release_record", {"issues": "write"}),
            ("workflow_dispatch", {"actions": "write"}),
            ("release_publish", {"contents": "write"}),
        ):
            with self.subTest(purpose=purpose):
                self.assertEqual(self.resolve(purpose), "installation-token")
                self.assertEqual(
                    self.requested[-1], {"repository_ids": [7001], "permissions": permissions}
                )

    def test_temporary_provider_failure_is_retryable_and_does_not_become_missing_configuration(
        self,
    ) -> None:
        headers = Message()
        headers["X-RateLimit-Remaining"] = "0"
        for code in (503, 429, 403):
            with self.subTest(code=code):
                self.provider_error = HTTPError(
                    "https://api.example/app", code, "provider error", headers, None
                )
                with self.assertRaises(DeliveryGitHubTokenUnavailable):
                    self.resolve("workflow_dispatch", retry_provider_errors=True)
        for error in (TimeoutError("slow provider"), ConnectionResetError("dropped connection")):
            with self.subTest(error=type(error).__name__):
                self.provider_error = error
                with self.assertRaises(DeliveryGitHubTokenUnavailable):
                    self.resolve("workflow_dispatch", retry_provider_errors=True)
                self.assertEqual(self.resolve("repository_read"), "")
        self.provider_error = HTTPError(
            "https://api.example/app", 404, "not installed", Message(), None
        )
        self.assertEqual(self.resolve("workflow_dispatch"), "")
        self.assertEqual(self.requested, [])

    def test_advisory_publisher_and_train_share_global_and_context_metadata(self) -> None:
        for context_id, expected in ((None, 77), ("88", 88)):
            with self.subTest(context_id=context_id):
                shared = RuntimeEnvironmentRecord(
                    scope="global",
                    env={ADVISORY_GITHUB_APP_ID_ENV_KEY: "77"},
                    updated_at="2026-10-05T14:00:00Z",
                )
                context = RuntimeEnvironmentRecord(
                    scope="context",
                    context="launchplane",
                    env={"SERVICE_DESCRIPTION": "test"}
                    if context_id is None
                    else {ADVISORY_GITHUB_APP_ID_ENV_KEY: context_id},
                    updated_at=shared.updated_at,
                )
                store = SimpleNamespace(
                    list_runtime_environment_records=lambda **kwargs: (
                        (context,) if kwargs.get("context_name") else (shared, context)
                    )
                )
                with (
                    patch(
                        "control_plane.github_app_identity.runtime_environments.load_runtime_environment_definition",
                        return_value=build_runtime_environment_definition_from_records(
                            (shared, context)
                        ),
                    ),
                    patch(
                        "control_plane.github_app_identity.control_plane_secrets.resolve_launchplane_service_secret",
                        return_value=self.key,
                    ),
                    patch(
                        "control_plane.runtime_environments.control_plane_secrets.overlay_runtime_environment_secret_values",
                        side_effect=AssertionError(
                            "App selectors must not decrypt a secret overlay"
                        ),
                    ),
                ):
                    identity = resolve_advisory_github_app_identity(control_plane_root=self.root)
                    self.assertEqual(identity.app_id, expected)
                    self.assertEqual(_owner_review_advisory_app_id(store), identity.app_id)

    def test_missing_accepted_grant_cannot_mint_or_use_a_pat(self) -> None:
        self.installation_permissions["issues"] = "read"
        self.assertEqual(self.resolve("release_record"), "")
        self.assertEqual(self.requested, [])

    def test_readiness_checks_configuration_without_minting_a_write_token(self) -> None:
        with (
            patch(
                "control_plane.launchplane_github_delivery.resolve_delivery_github_app_identity",
                return_value=self.identity,
            ),
            patch(
                "control_plane.launchplane_github_delivery.mint_delivery_installation_token",
                side_effect=AssertionError("Readiness must not mint a token"),
            ),
        ):
            self.assertTrue(
                delivery_github_credentials_ready(
                    control_plane_root=self.root, repository="example/site"
                )
            )
            self.assertFalse(
                delivery_github_credentials_ready(
                    control_plane_root=self.root, repository="example/missing"
                )
            )

    def test_missing_or_retired_inventory_cannot_mint(self) -> None:
        self.assertEqual(self.resolve("repository_read", repository="example/missing"), "")
        self.store.write_repository_inventory_record(
            _inventory(
                repository="example/site",
                inventory_state="retired",
                inventory_revision=2,
                supersedes_record_id=_inventory(repository="example/site").record_id,
            )
        )
        self.assertEqual(self.resolve("repository_read"), "")
        self.assertEqual(self.requested, [])

    def test_wrong_repository_or_excess_token_permissions_are_rejected_and_revoked(self) -> None:
        for wrong_name, extra in [("example/other", {}), ("example/site", {"contents": "write"})]:
            with self.subTest(repository=wrong_name, extra=extra):
                self.token_repository = wrong_name
                self.extra_token_permissions = extra
                self.revoked = False
                self.assertEqual(self.resolve("release_record"), "")
                self.assertTrue(self.revoked)

    def test_identity_reuses_exact_managed_binding_selected_in_runtime_records(self) -> None:
        with (
            patch(
                "control_plane.launchplane_github_delivery.runtime_environments.load_runtime_environment_definition",
                return_value=RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "launchplane": RuntimeEnvironmentContextDefinition(
                            shared_env={
                                DELIVERY_GITHUB_APP_ID_KEY: "76",
                                DELIVERY_GITHUB_APP_INTEGRATION_KEY: "existing-delivery-key",
                                "GITHUB_TOKEN": "old-pat",
                            },
                            instances={},
                        )
                    },
                ),
            ),
            patch(
                "control_plane.launchplane_github_delivery.secrets.resolve_context_secret_value",
                return_value=self.key,
            ) as key,
        ):
            identity = resolve_delivery_github_app_identity(control_plane_root=self.root)
        self.assertEqual(identity.app_id, 76)
        key.assert_called_once_with(
            integration="existing-delivery-key",
            context_name="launchplane",
            binding_key="private_key",
        )

    def test_legacy_pat_setting_or_secret_never_resolves_without_app_identity(self) -> None:
        with (
            patch(
                "control_plane.launchplane_github_delivery.runtime_environments.load_runtime_environment_definition",
                return_value=RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "launchplane": RuntimeEnvironmentContextDefinition(
                            shared_env={"GITHUB_TOKEN": "old-pat"}, instances={}
                        )
                    },
                ),
            ),
            patch(
                "control_plane.launchplane_github_delivery.secrets.resolve_context_secret_value"
            ) as key,
        ):
            self.assertEqual(
                resolve_launchplane_github_token(
                    control_plane_root=self.root, context_name="site", repository="example/site"
                ),
                "",
            )
        key.assert_not_called()

    def test_historical_train_context_source_cannot_resolve_delivery_token(self) -> None:
        with patch(
            "control_plane.launchplane_github_delivery.resolve_delivery_github_token"
        ) as delivery:
            self.assertEqual(
                resolve_merge_train_github_token(
                    source=MergeTrainGitHubTokenSource(runtime_context="site"),
                    control_plane_root=self.root,
                    repository="example/site",
                ),
                "",
            )
        delivery.assert_not_called()
