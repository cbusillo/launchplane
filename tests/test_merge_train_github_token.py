from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from control_plane import secrets
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.merge_train_policy import (
    MergeTrainGitHubAppSource,
    MergeTrainGitHubTokenSource,
)
from control_plane.merge_train_github_token import resolve_merge_train_github_token


class MergeTrainGitHubTokenTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def setUp(self) -> None:
        self.source = MergeTrainGitHubTokenSource(
            github_app=MergeTrainGitHubAppSource(
                app_id=42, repository_id=123, private_key_context="example_context"
            )
        )
        self.calls: list[dict[str, object]] = []
        self.minted = 0
        self.returned_repository = "example/repo"
        self.token_permissions = {
            "checks": "read",
            "contents": "write",
            "metadata": "read",
            "pull_requests": "write",
            "statuses": "read",
            "workflows": "write",
        }
        self.installation_permissions = {
            **self.token_permissions,
            "actions": "write",
            "issues": "write",
            "administration": "read",
            "security_events": "read",
            "vulnerability_alerts": "read",
            "secret_scanning_alerts": "read",
            "deployments": "read",
            "repository_projects": "admin",
        }

    def provider(self, _request: object, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        path = kwargs["path"]
        assert isinstance(path, str)
        if path == "/app":
            return {"id": 42}
        if path == "/repos/example/repo/installation":
            return {
                "id": 77,
                "app_id": 42,
                "permissions": self.installation_permissions,
            }
        if path == "/installation/token" and kwargs["method"] == "DELETE":
            return None
        if path != "/app/installations/77/access_tokens" or kwargs["method"] != "POST":
            raise AssertionError(f"Unexpected provider request: {path}")
        self.minted += 1
        return {
            "token": f"example-installation-token-{self.minted}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "permissions": self.token_permissions,
            "repositories": [{"id": 123, "full_name": self.returned_repository}],
        }

    def resolve(self) -> str:
        return resolve_merge_train_github_token(
            source=self.source,
            repository="example/repo",
            control_plane_root=Path("/unused"),
        )

    def test_app_source_renews_exact_repository_tokens_from_managed_key(self) -> None:
        with (
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=self.private_key,
            ) as managed_key,
            patch(
                "control_plane.github_app_identity._github_api_request", side_effect=self.provider
            ),
        ):
            first, second = self.resolve(), self.resolve()

        self.assertNotEqual(first, second)
        self.assertEqual(self.minted, 2)
        managed_key.assert_called_with(
            integration="merge_train_github_app",
            context_name="example_context",
            binding_key="private_key",
        )
        mint = next(call for call in self.calls if call.get("method") == "POST")
        self.assertEqual(
            mint["body"],
            {
                "repository_ids": [123],
                "permissions": {
                    "checks": "read",
                    "contents": "write",
                    "pull_requests": "write",
                    "statuses": "read",
                    "workflows": "write",
                },
            },
        )

    def test_shared_installation_write_grants_are_downscoped_for_observation(self) -> None:
        self.installation_permissions.update(checks="write", statuses="write")
        with (
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=self.private_key,
            ),
            patch(
                "control_plane.github_app_identity._github_api_request", side_effect=self.provider
            ),
        ):
            self.assertEqual(self.resolve(), "example-installation-token-1")
        mint = next(call for call in self.calls if call.get("method") == "POST")
        body = mint["body"]
        assert isinstance(body, dict)
        permissions = body["permissions"]
        self.assertEqual(permissions["checks"], "read")
        self.assertEqual(permissions["statuses"], "read")
        self.assertNotIn("actions", permissions)
        self.assertNotIn("administration", permissions)

    def test_missing_or_invalid_installation_capability_fails_before_mint(self) -> None:
        for permission, value in (
            ("workflows", "read"),
            ("contents", "read"),
            ("actions", "invalid"),
        ):
            with self.subTest(permission=permission, value=value):
                self.setUp()
                self.installation_permissions[permission] = value
                with (
                    patch.dict("os.environ", {"GH_TOKEN": "must-not-be-used"}),
                    patch(
                        "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                        return_value=self.private_key,
                    ),
                    patch(
                        "control_plane.github_app_identity._github_api_request",
                        side_effect=self.provider,
                    ),
                    self.assertLogs(
                        "control_plane.merge_train_github_token", level="WARNING"
                    ) as logs,
                ):
                    self.assertEqual(self.resolve(), "")
                if permission in {"workflows", "contents"}:
                    self.assertIn(f"missing_grants ({permission}:write)", logs.output[-1])
                self.assertNotIn(self.private_key, "".join(logs.output))
                self.assertEqual(self.minted, 0)
                self.assertFalse(any(call.get("method") == "POST" for call in self.calls))

    def test_wrong_repository_or_excess_token_authority_is_revoked_without_fallback(self) -> None:
        for failure in ("repository", "administration", "actions", "workflow_permission"):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "repository":
                    self.returned_repository = "example/another-product"
                elif failure == "workflow_permission":
                    self.token_permissions["workflows"] = "read"
                else:
                    self.token_permissions[failure] = "write"
                with (
                    patch.dict("os.environ", {"GH_TOKEN": "must-not-be-used"}),
                    patch(
                        "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                        return_value=self.private_key,
                    ),
                    patch(
                        "control_plane.github_app_identity._github_api_request",
                        side_effect=self.provider,
                    ),
                ):
                    self.assertEqual(self.resolve(), "")
                self.assertEqual(self.calls[-1]["path"], "/installation/token")
                self.assertEqual(self.calls[-1]["method"], "DELETE")

    def test_missing_key_fails_before_provider_access_and_sources_cannot_mix(self) -> None:
        with (
            patch.dict("os.environ", {"GH_TOKEN": "must-not-be-used"}),
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value="",
            ),
            patch("control_plane.github_app_identity._github_api_request") as provider,
        ):
            self.assertEqual(self.resolve(), "")
        provider.assert_not_called()
        for competing_source in ({"env_var": "GH_TOKEN"}, {"runtime_context": "example_context"}):
            with self.subTest(source=competing_source), self.assertRaises(ValidationError):
                MergeTrainGitHubTokenSource(github_app=self.source.github_app, **competing_source)

    def test_real_store_does_not_inherit_global_keys_or_choose_duplicate_context_bindings(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'secrets.db'}"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "LAUNCHPLANE_DATABASE_URL": database_url,
                            secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                        },
                        clear=True,
                    ),
                    patch(
                        "control_plane.github_app_identity._github_api_request",
                        side_effect=self.provider,
                    ),
                ):
                    secrets.write_secret_value(
                        record_store=store,
                        scope="global",
                        integration="merge_train_github_app",
                        name="global-key",
                        binding_key="private_key",
                        plaintext_value=self.private_key,
                        actor="test",
                    )
                    self.assertEqual(self.resolve(), "")
                    self.assertEqual(self.calls, [])
                    secrets.write_secret_value(
                        record_store=store,
                        scope="context",
                        context_name="example_context",
                        integration="merge_train_github_app",
                        name="context-key",
                        binding_key="private_key",
                        plaintext_value=self.private_key,
                        actor="test",
                    )
                    self.assertEqual(self.resolve(), "example-installation-token-1")
                    secrets.write_secret_value(
                        record_store=store,
                        scope="context",
                        context_name="example_context",
                        integration="merge_train_github_app",
                        name="duplicate-key",
                        binding_key="private_key",
                        plaintext_value=self.private_key,
                        actor="test",
                    )
                    self.assertEqual(self.resolve(), "")
                    self.assertEqual(self.minted, 1)
            finally:
                store.close()
