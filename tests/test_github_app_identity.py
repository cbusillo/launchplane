from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import unittest
import click

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    GitHubAppInstallationToken,
    inspect_ordinary_agent_github_app_installation,
    mint_ordinary_agent_installation_token,
    mint_repository_installation_token,
    revoke_installation_token,
)


class GitHubAppIdentityTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def test_mints_exact_repository_scoped_checks_token(self) -> None:
        observed_jwts: list[str] = []

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            token = kwargs["token"]
            if kwargs["path"] != "/app/installations/77/access_tokens":
                observed_jwts.append(token)
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {"checks": "write", "metadata": "read"},
                }
            self.assertEqual(
                kwargs["body"],
                {"repository_ids": [123], "permissions": {"checks": "write"}},
            )
            return {
                "token": "installation-token-secret",
                "expires_at": "2026-08-07T15:00:00Z",
                "permissions": {"checks": "write", "metadata": "read"},
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            }

        result = mint_repository_installation_token(
            identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
            repository="example/repo",
            repository_id="123",
            api_request=api_request,
            now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.app_id, 42)
        self.assertEqual(result.installation_id, 77)
        self.assertEqual(result.repository_id, 123)
        self.assertNotIn("installation-token-secret", repr(result))
        claims = jwt.decode(observed_jwts[0], options={"verify_signature": False})
        self.assertEqual(claims["iss"], "42")
        self.assertEqual(claims["exp"] - claims["iat"], 540)

    def test_reconciliation_mint_attenuates_writes_and_rejects_provider_escalation(self) -> None:
        for returned_access in ("read", "write"):
            with self.subTest(returned_access=returned_access):

                def api_request(**kwargs: object) -> object:
                    if kwargs["path"] == "/repos/example/repo/installation":
                        return {
                            "id": 77,
                            "app_id": 42,
                            "permissions": {
                                "administration": "read",
                                "checks": "read",
                                "contents": "write",
                                "metadata": "read",
                                "pull_requests": "write",
                                "statuses": "read",
                            },
                        }
                    self.assertEqual(
                        kwargs["body"],
                        {
                            "repository_ids": [123],
                            "permissions": {"contents": "read", "pull_requests": "read"},
                        },
                    )
                    return {
                        "token": "test-recovery-token",
                        "expires_at": "2026-08-07T15:00:00Z",
                        "permissions": {
                            "contents": returned_access,
                            "metadata": "read",
                            "pull_requests": "read",
                        },
                        "repositories": [{"id": 123, "full_name": "example/repo"}],
                    }

                def mint() -> GitHubAppInstallationToken:
                    return mint_ordinary_agent_installation_token(
                        identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                        repository="example/repo",
                        repository_id="123",
                        effect_profile="effect_reconciliation",
                        api_request=api_request,
                        now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
                    )

                if returned_access == "write":
                    with self.assertRaises(GitHubAppIdentityError):
                        mint()
                else:
                    self.assertTrue(
                        all(permission.endswith(":read") for permission in mint().permissions)
                    )

    def test_head_refresh_uses_its_closed_permission_profile(self) -> None:
        observed_body: dict[str, object] = {}

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            observed_body.update(kwargs["body"])
            return {
                "token": "installation-token-secret",
                "expires_at": "2026-08-07T15:00:00Z",
                "permissions": {
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                },
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            }

        result = mint_ordinary_agent_installation_token(
            identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
            repository="example/repo",
            repository_id="123",
            effect_profile="head_refresh",
            api_request=api_request,
            now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            observed_body,
            {
                "repository_ids": [123],
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
        )
        self.assertEqual(result.permissions, ("contents:write", "pull_requests:write"))

    def test_snapshot_uses_only_the_read_profile_under_the_full_app_ceiling(self) -> None:
        observed_body: dict[str, object] = {}
        paths: list[str] = []

        def api_request(**kwargs: object) -> object:
            paths.append(str(kwargs["path"]))
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            body = kwargs["body"]
            assert isinstance(body, dict)
            observed_body.update(body)
            return {
                "token": "snapshot-token-secret",
                "expires_at": "2026-08-07T15:00:00Z",
                "permissions": {
                    "administration": "read",
                    "checks": "read",
                    "contents": "read",
                    "metadata": "read",
                    "pull_requests": "read",
                    "statuses": "read",
                },
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            }

        result = mint_ordinary_agent_installation_token(
            identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
            repository="example/repo",
            repository_id="123",
            effect_profile="merge_train_snapshot",
            api_request=api_request,
            now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            paths, ["/repos/example/repo/installation", "/app/installations/77/access_tokens"]
        )
        self.assertEqual(
            observed_body["permissions"],
            {
                "administration": "read",
                "checks": "read",
                "contents": "read",
                "pull_requests": "read",
                "statuses": "read",
            },
        )
        self.assertEqual(
            result.permissions,
            (
                "administration:read",
                "checks:read",
                "contents:read",
                "pull_requests:read",
                "statuses:read",
            ),
        )

    def test_ordinary_mint_checks_discovered_installation_wait_before_post(self) -> None:
        calls: list[str] = []

        def api_request(**kwargs: object) -> object:
            path = str(kwargs["path"])
            calls.append(path)
            if path == "/app":
                return {"id": 42}
            if path == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            self.fail("token POST must remain undispatched")

        def block_mint(app_id: int, installation_id: int) -> None:
            self.assertEqual((app_id, installation_id), (42, 77))
            raise RuntimeError("provider_wait")

        with self.assertRaisesRegex(RuntimeError, "provider_wait"):
            mint_ordinary_agent_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                effect_profile="merge_train_snapshot",
                api_request=api_request,
                now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
                before_token_mint=block_mint,
            )

        self.assertEqual(calls, ["/repos/example/repo/installation"])

    def test_ordinary_installation_identity_is_required_before_mint(self) -> None:
        for app_id in (99, None, True, 0, "42"):
            with self.subTest(app_id=app_id):
                calls: list[str] = []

                def api_request(**kwargs: object) -> object:
                    calls.append(str(kwargs["path"]))
                    return {"id": 77, "app_id": app_id}

                with self.assertRaises(GitHubAppIdentityError):
                    mint_ordinary_agent_installation_token(
                        identity=GitHubAppIdentity(
                            app_id=1 if app_id is True else 42, private_key=self.private_key
                        ),
                        repository="example/repo",
                        repository_id="123",
                        effect_profile="merge_train_snapshot",
                        api_request=api_request,
                    )
                self.assertEqual(calls, ["/repos/example/repo/installation"])

    def test_ordinary_authentication_failure_never_mints_or_falls_back(self) -> None:
        calls: list[str] = []

        def api_request(**kwargs: object) -> object:
            calls.append(str(kwargs["path"]))
            raise click.ClickException("GitHub App JWT authentication failed: HTTP 401")

        with self.assertRaisesRegex(GitHubAppIdentityError, "authentication.*401"):
            mint_ordinary_agent_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                effect_profile="merge_train_snapshot",
                api_request=api_request,
            )
        self.assertEqual(calls, ["/repos/example/repo/installation"])

    def test_inspects_ordinary_agent_installation_without_minting(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "account": {"id": 456, "login": "example"},
                    "permissions": {
                        "administration": "read",
                        "checks": "read",
                        "contents": "write",
                        "metadata": "read",
                        "pull_requests": "write",
                        "statuses": "read",
                    },
                }
            raise AssertionError(kwargs["path"])

        result = inspect_ordinary_agent_github_app_installation(
            identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
            repository="example/repo",
            repository_id="123",
            repository_owner_id="456",
            api_request=api_request,
            now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.app_id, 42)
        self.assertEqual(result.installation_id, 77)
        self.assertEqual(result.repository_id, 123)
        self.assertEqual(result.repository_owner_id, 456)
        self.assertEqual(result.repository, "example/repo")
        self.assertEqual(
            result.permissions,
            (
                "administration:read",
                "checks:read",
                "contents:write",
                "metadata:read",
                "pull_requests:write",
                "statuses:read",
            ),
        )
        self.assertEqual(
            tuple(call["path"] for call in calls),
            (
                "/app",
                "/repos/example/repo/installation",
            ),
        )
        self.assertTrue(all("method" not in call for call in calls))

    def test_inspection_rejects_owner_or_permission_drift(self) -> None:
        def inspect(*, account: object, permissions: object) -> None:
            def api_request(**kwargs: object) -> object:
                if kwargs["path"] == "/app":
                    return {"id": 42}
                return {
                    "id": 77,
                    "app_id": 42,
                    "account": account,
                    "permissions": permissions,
                }

            inspect_ordinary_agent_github_app_installation(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                repository_owner_id="456",
                api_request=api_request,
            )

        with self.assertRaisesRegex(GitHubAppIdentityError, "inventory owner"):
            inspect(
                account={"id": 999, "login": "example"},
                permissions={
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                },
            )
        with self.assertRaisesRegex(GitHubAppIdentityError, "beyond"):
            inspect(
                account={"id": 456, "login": "example"},
                permissions={
                    "administration": "read",
                    "checks": "read",
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                    "statuses": "read",
                    "workflows": "write",
                },
            )

    def test_revokes_installation_token_without_exposing_secret(self) -> None:
        calls: list[dict[str, object]] = []
        token = GitHubAppInstallationToken(
            token="installation-token-secret",
            app_id=42,
            installation_id=77,
            repository_id=123,
            repository="example/repo",
            expires_at="2026-08-07T15:00:00Z",
        )

        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            return None

        revoke_installation_token(
            installation_token=token,
            api_request=api_request,
        )

        self.assertEqual(calls[0]["path"], "/installation/token")
        self.assertEqual(calls[0]["method"], "DELETE")
        self.assertNotIn("installation-token-secret", repr(token))

    def test_rejects_installation_from_another_app(self) -> None:
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["path"] == "/app":
                return {"id": 42}
            return {
                "id": 77,
                "app_id": 99,
                "permissions": {"checks": "write"},
            }

        with self.assertRaisesRegex(GitHubAppIdentityError, "another app"):
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                api_request=api_request,
            )

    def test_rejects_invalid_private_key_without_api_call(self) -> None:
        calls: list[dict[str, object]] = []

        with self.assertRaisesRegex(GitHubAppIdentityError, "private key is invalid"):
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key="not-a-private-key"),
                repository="example/repo",
                repository_id="123",
                api_request=lambda **kwargs: calls.append(kwargs),
            )

        self.assertEqual(calls, [])

    def test_rejects_surplus_installation_permissions(self) -> None:
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["path"] == "/app":
                return {"id": 42}
            return {
                "id": 77,
                "app_id": 42,
                "permissions": {"checks": "write", "contents": "read"},
            }

        with self.assertRaisesRegex(GitHubAppIdentityError, "beyond"):
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                api_request=api_request,
            )

    def test_rejects_expired_installation_token(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            if kwargs["path"] == "/installation/token":
                return None
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {"checks": "write"},
                }
            return {
                "token": "expired-token",
                "expires_at": "2026-08-07T14:00:30Z",
                "permissions": {"checks": "write"},
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            }

        with self.assertRaisesRegex(GitHubAppIdentityError, "safely in the future"):
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                api_request=api_request,
                now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(calls[-1]["path"], "/installation/token")
        self.assertEqual(calls[-1]["method"], "DELETE")
        self.assertEqual(calls[-1]["token"], "expired-token")

    def test_revokes_token_after_post_mint_validation_failures(self) -> None:
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "malformed expiry",
                {
                    "token": "malformed-expiry-token",
                    "expires_at": "not-a-timestamp",
                    "permissions": {"checks": "write"},
                    "repositories": [{"id": 123, "full_name": "example/repo"}],
                },
                "expiry is malformed",
            ),
            (
                "surplus permission",
                {
                    "token": "surplus-permission-token",
                    "expires_at": "2026-08-07T15:00:00Z",
                    "permissions": {"checks": "write", "contents": "read"},
                    "repositories": [{"id": 123, "full_name": "example/repo"}],
                },
                "beyond advisory check projection",
            ),
            (
                "repository count",
                {
                    "token": "repository-count-token",
                    "expires_at": "2026-08-07T15:00:00Z",
                    "permissions": {"checks": "write"},
                    "repositories": [],
                },
                "exactly one repository",
            ),
            (
                "repository id",
                {
                    "token": "repository-id-token",
                    "expires_at": "2026-08-07T15:00:00Z",
                    "permissions": {"checks": "write"},
                    "repositories": [{"id": 999, "full_name": "example/repo"}],
                },
                "exact repository id",
            ),
            (
                "repository name",
                {
                    "token": "repository-name-token",
                    "expires_at": "2026-08-07T15:00:00Z",
                    "permissions": {"checks": "write"},
                    "repositories": [{"id": 123, "full_name": "example/other"}],
                },
                "exact repository name",
            ),
        )
        for label, token_payload, error_pattern in cases:
            with self.subTest(label=label):
                calls: list[dict[str, object]] = []
                api_request = self._token_api_request(
                    token_payload=token_payload,
                    calls=calls,
                )

                with self.assertRaisesRegex(GitHubAppIdentityError, error_pattern):
                    mint_repository_installation_token(
                        identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                        repository="example/repo",
                        repository_id="123",
                        api_request=api_request,
                        now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
                    )

                self.assertEqual(calls[-1]["path"], "/installation/token")
                self.assertEqual(calls[-1]["method"], "DELETE")
                self.assertEqual(calls[-1]["token"], token_payload["token"])

    def test_preserves_validation_error_when_post_mint_revocation_fails(self) -> None:
        calls: list[dict[str, object]] = []
        api_request = self._token_api_request(
            token_payload={
                "token": "invalid-scope-token",
                "expires_at": "2026-08-07T15:00:00Z",
                "permissions": {"checks": "write"},
                "repositories": [],
            },
            calls=calls,
            revocation_response={"unexpected": "payload"},
        )

        with self.assertRaisesRegex(GitHubAppIdentityError, "exactly one repository") as caught:
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                api_request=api_request,
                now=datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(calls[-1]["path"], "/installation/token")
        self.assertTrue(
            any("revocation also failed" in note for note in caught.exception.__notes__)
        )

    def test_does_not_revoke_without_usable_minted_token(self) -> None:
        calls: list[dict[str, object]] = []
        api_request = self._token_api_request(
            token_payload={
                "expires_at": "2026-08-07T15:00:00Z",
                "permissions": {"checks": "write"},
                "repositories": [{"id": 123, "full_name": "example/repo"}],
            },
            calls=calls,
        )

        with self.assertRaisesRegex(GitHubAppIdentityError, "requires token"):
            mint_repository_installation_token(
                identity=GitHubAppIdentity(app_id=42, private_key=self.private_key),
                repository="example/repo",
                repository_id="123",
                api_request=api_request,
            )

        self.assertNotIn("/installation/token", tuple(call["path"] for call in calls))

    @staticmethod
    def _token_api_request(
        *,
        token_payload: dict[str, object],
        calls: list[dict[str, object]],
        revocation_response: object = None,
    ) -> Callable[..., object]:
        def api_request(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            if kwargs["path"] == "/installation/token":
                return revocation_response
            if kwargs["path"] == "/app":
                return {"id": 42}
            if kwargs["path"] == "/repos/example/repo/installation":
                return {
                    "id": 77,
                    "app_id": 42,
                    "permissions": {"checks": "write"},
                }
            return token_payload

        return api_request


if __name__ == "__main__":
    unittest.main()
