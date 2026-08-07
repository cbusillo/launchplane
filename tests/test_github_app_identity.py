from __future__ import annotations

from datetime import datetime, timezone
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    GitHubAppInstallationToken,
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
        def api_request(**kwargs):  # type: ignore[no-untyped-def]
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


if __name__ == "__main__":
    unittest.main()
