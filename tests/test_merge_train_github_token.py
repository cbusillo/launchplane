from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.message import Message
from urllib.error import HTTPError
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

import click

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from control_plane import merge_train_planning_completion as completion
from control_plane import secrets
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.merge_train_policy import (
    MergeTrainGitHubAppSource,
    MergeTrainGitHubTokenSource,
)
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    mint_build_provenance_installation_token,
    mint_pull_request_feedback_installation_token,
)
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.github_read_cache import GitHubReadCache
from control_plane.merge_train_github import MergeTrainGitHubError, UrllibMergeTrainGitHubTransport
from tests.test_merge_train_read_cost import _Provider
from tests.test_merge_train_planning_completion import (
    PlanningProvider,
    _BOT,
    _LABELS,
    _REPO,
    _receipt,
)
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
)


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
        cache_patch = patch.dict("control_plane.merge_train_github_token._TOKEN_CACHE", clear=True)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.source = MergeTrainGitHubTokenSource(
            github_app=MergeTrainGitHubAppSource(
                app_id=42, repository_id=123, private_key_context="example_context"
            )
        )
        self.calls: list[dict[str, object]] = []
        self.minted = 0
        self.token_lifetime = timedelta(hours=1)
        self.repository = "example/repo"
        self.returned_repository = self.repository
        self.token_permissions = {
            "checks": "read",
            "contents": "write",
            "metadata": "read",
            "issues": "write",
            "pull_requests": "write",
            "statuses": "read",
            "workflows": "write",
        }
        self.installation_permissions = {
            **self.token_permissions,
            "actions": "write",
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
        if path == f"/repos/{self.repository}/installation":
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
            "expires_at": (datetime.now(timezone.utc) + self.token_lifetime).isoformat(),
            "permissions": self.token_permissions,
            "repositories": [{"id": 123, "full_name": self.returned_repository}],
        }

    def resolve(self) -> str:
        return resolve_merge_train_github_token(
            source=self.source,
            repository=self.repository,
            control_plane_root=Path("/unused"),
        )

    def test_historical_env_source_cannot_resolve_environment_token(self) -> None:
        with patch.dict("os.environ", {"GH_TOKEN": "must-not-be-used"}):
            self.assertEqual(
                resolve_merge_train_github_token(
                    source=MergeTrainGitHubTokenSource(env_var="GH_TOKEN"),
                    control_plane_root=Path("."),
                    repository="example/repo",
                ),
                "",
            )

    def test_app_source_reuses_unexpired_exact_repository_tokens_from_managed_key(self) -> None:
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

        self.assertEqual(first, second)
        self.assertEqual(self.minted, 1)
        managed_key.assert_called_with(
            integration="merge_train_github_app",
            context_name="example_context",
            binding_key="private_key",
        )
        mint = next(call for call in self.calls if call.get("method") == "POST")
        assert self.source.github_app is not None
        body = mint["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["repository_ids"], [self.source.github_app.repository_id])

    def test_minted_native_token_can_complete_planning_label_projection(self) -> None:
        self.repository = self.returned_repository = _REPO
        planning = PlanningProvider()

        def provider(request: object, **kwargs: object) -> object:
            if kwargs.get("method", "GET") == "POST":
                body = kwargs["body"]
                assert isinstance(body, dict)
                # Model the provider's actual downscoping, not the App's wider grant.
                self.token_permissions = {"metadata": "read", **body["permissions"]}
            return self.provider(request, **kwargs)

        def planning_request(**kwargs: Any) -> object:
            if kwargs.get("method", "GET") != "GET" and "/labels" in str(kwargs["path"]):
                if self.token_permissions.get("issues") != "write":
                    raise click.ClickException("Issue-label capability refused") from HTTPError(
                        str(kwargs["path"]), 403, "Forbidden", Message(), None
                    )
            self.assertEqual(kwargs["token"], issued)
            return planning.request(**kwargs)

        with (
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=self.private_key,
            ),
            patch("control_plane.github_app_identity._github_api_request", side_effect=provider),
            patch.object(completion, "github_api_request", side_effect=planning_request),
            patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                return_value={"action": "updated_comment", "comment_id": 3, "comment_url": ""},
            ),
        ):
            issued = self.resolve()
            record = build_merge_train_pr_feedback_record(
                request=MergeTrainPrFeedbackEnvelope(
                    repository=self.repository,
                    pull_request_number=9,
                    event="completed",
                    controller_action="land_batch",
                ),
                policy_key="test",
                policy_sha256="digest",
                token=issued,
                recorded_at="2026-10-01T00:00:00Z",
                response_trace_id="minted-planning-completion",
                planning_author_ids=(_BOT,),
            )
        self.assertEqual(record.delivery_status, "delivered")
        self.assertTrue(_receipt(record)["ok"])
        self.assertEqual(
            {label["name"] for label in planning.issue["labels"]},
            {_LABELS["plan"], _LABELS["done"], "bug"},
        )

    def test_cached_native_tokens_reuse_validators_until_key_rotation(
        self,
    ) -> None:
        cache = GitHubReadCache()
        http = _Provider()
        with (
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=self.private_key,
            ),
            patch(
                "control_plane.github_app_identity._github_api_request", side_effect=self.provider
            ),
            patch("control_plane.merge_train_github.urlopen", http),
        ):
            tokens = [self.resolve(), self.resolve()]
            for token in tokens:
                UrllibMergeTrainGitHubTransport(token=token, read_cache=cache).request(
                    method="GET", path="/value"
                )
            self.assertEqual(tokens[0], tokens[1])
            self.assertEqual(self.minted, 1)
            self.assertEqual(http.used, 1)
            other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            other_pem = other_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            with patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=other_pem,
            ):
                rotated = self.resolve()
            UrllibMergeTrainGitHubTransport(token=rotated, read_cache=cache).request(
                method="GET", path="/value"
            )
            self.assertEqual(http.used, 2)

    def test_auth_failures_refresh_issued_token_but_quota_failures_preserve_it(self) -> None:
        for status, quota_limited in ((401, False), (403, False), (403, True)):
            with (
                self.subTest(status=status, quota_limited=quota_limited),
                patch.dict("control_plane.merge_train_github_token._TOKEN_CACHE", {}, clear=True),
                patch(
                    "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                    return_value=self.private_key,
                ),
                patch(
                    "control_plane.github_app_identity._github_api_request",
                    side_effect=self.provider,
                ),
            ):
                first = self.resolve()
                headers = Message()
                if quota_limited:
                    headers["X-RateLimit-Remaining"] = "0"
                    headers["X-RateLimit-Reset"] = "1800000000"
                failure = HTTPError("https://api.github.com/value", status, "denied", headers, None)
                with patch("control_plane.merge_train_github.urlopen", side_effect=failure):
                    with self.assertRaises(MergeTrainGitHubError) as raised:
                        UrllibMergeTrainGitHubTransport(token=first).request(
                            method="GET", path="/value"
                        )
                self.assertEqual(raised.exception.rate_limited, quota_limited)
                self.assertEqual(self.resolve() == first, status != 401)

    def test_native_token_refreshes_before_expiry_and_repository_scope_does_not_share(self) -> None:
        with (
            patch(
                "control_plane.merge_train_github_token.secrets.resolve_context_secret_value",
                return_value=self.private_key,
            ),
            patch(
                "control_plane.github_app_identity._github_api_request", side_effect=self.provider
            ),
        ):
            self.token_lifetime = timedelta(minutes=4)
            first = self.resolve()
            self.token_lifetime = timedelta(hours=1)
            renewed = self.resolve()
            self.assertNotEqual(first, renewed)
            self.assertEqual(self.minted, 2)
            # A different repository identity cannot borrow the cached token.
            assert self.source.github_app is not None
            self.source.github_app.repository_id = 124
            self.assertEqual(self.resolve(), "")

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
            ("issues", "read"),
            ("issues", "absent"),
            ("actions", "invalid"),
        ):
            with self.subTest(permission=permission, value=value):
                self.setUp()
                if value == "absent":
                    self.installation_permissions.pop(permission)
                else:
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
                if permission in {"workflows", "contents", "issues"}:
                    self.assertIn(f"{permission}:write", logs.output[-1])
                self.assertNotIn(self.private_key, "".join(logs.output))
                self.assertEqual(self.minted, 0)
                self.assertFalse(any(call.get("method") == "POST" for call in self.calls))

    def test_wrong_repository_or_excess_token_authority_is_revoked_without_fallback(self) -> None:
        for failure in (
            "repository",
            "administration",
            "actions",
            "workflow_permission",
            "issues_permission",
        ):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "repository":
                    self.returned_repository = "example/another-product"
                elif failure in {"workflow_permission", "issues_permission"}:
                    self.token_permissions[
                        "workflows" if failure == "workflow_permission" else "issues"
                    ] = "read"
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

    def test_shared_train_installation_mints_a_read_only_build_token(self) -> None:
        self.token_permissions = {
            "actions": "read",
            "contents": "read",
            "metadata": "read",
            "pull_requests": "read",
        }
        with patch(
            "control_plane.github_app_identity._github_api_request", side_effect=self.provider
        ):
            token = mint_build_provenance_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
            )

        self.assertEqual(token.token, "example-installation-token-1")
        mint = next(call for call in self.calls if call.get("method") == "POST")
        self.assertEqual(
            mint["body"],
            {
                "repository_ids": [123],
                "permissions": {"actions": "read", "contents": "read", "pull_requests": "read"},
            },
        )

    def test_shared_train_installation_mints_a_comment_only_feedback_token(self) -> None:
        self.token_permissions = {"contents": "read", "metadata": "read", "pull_requests": "write"}
        with patch(
            "control_plane.github_app_identity._github_api_request", side_effect=self.provider
        ):
            token = mint_pull_request_feedback_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
            )

        self.assertEqual(token.token, "example-installation-token-1")
        mint = next(call for call in self.calls if call.get("method") == "POST")
        # Narrower than the train's own token: no contents or workflow writes.
        self.assertEqual(
            mint["body"],
            {
                "repository_ids": [123],
                "permissions": {"contents": "read", "pull_requests": "write"},
            },
        )
