"""Server-only OAuth custody, bound to one browser session and immutable person."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


@dataclass(frozen=True)
class GitHubOAuthCredential:
    access_token: str = field(repr=False)
    access_expires_at: datetime | None = None
    refresh_token: str = field(default="", repr=False)
    refresh_expires_at: datetime | None = None

    @classmethod
    def from_token(cls, token: Any, *, now: datetime) -> GitHubOAuthCredential:
        if not isinstance(token, dict) or token.get("error"):
            raise ValueError("GitHub OAuth token response is invalid.")
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token", "")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or str(token.get("token_type", "")).lower() != "bearer"
        ):
            raise ValueError("GitHub OAuth token response is invalid.")

        def deadline(key: str) -> datetime | None:
            value = token.get(key)
            if value is None:
                return None
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
                raise ValueError("GitHub OAuth token lifetime is invalid.")
            seconds = int(value)
            if seconds <= 0:
                raise ValueError("GitHub OAuth token lifetime is invalid.")
            return now + timedelta(seconds=seconds)

        access_expires_at = deadline("expires_in")
        refresh_expires_at = deadline("refresh_token_expires_in")
        if bool(refresh_token) != (refresh_expires_at is not None):
            raise ValueError("GitHub OAuth refresh lifetime is missing.")
        return cls(access_token, access_expires_at, refresh_token, refresh_expires_at)


class HumanCredentialCipher:
    def __init__(self, session_secret: str) -> None:
        # Domain separation keeps the existing bootstrap root usable without a
        # new secret or a manual production rotation. Never use cookie HMAC keys
        # directly as encryption keys.
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"launchplane/human-oauth-credential/v1",
        ).derive(session_secret.encode())
        self._cipher = AESGCM(key)

    @staticmethod
    def _binding(session_id: str, github_id: int) -> bytes:
        return f"launchplane/human-oauth/v1/{session_id}/{github_id}".encode()

    def encrypt(self, credential: GitHubOAuthCredential, *, session_id: str, github_id: int) -> str:
        payload = json.dumps(
            {
                "access_token": credential.access_token,
                "access_expires_at": (
                    credential.access_expires_at.isoformat()
                    if credential.access_expires_at
                    else None
                ),
                "refresh_token": credential.refresh_token,
                "refresh_expires_at": (
                    credential.refresh_expires_at.isoformat()
                    if credential.refresh_expires_at
                    else None
                ),
            }
        ).encode()
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, payload, self._binding(session_id, github_id))
        return "v1." + base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, ciphertext: str, *, session_id: str, github_id: int) -> GitHubOAuthCredential:
        if not ciphertext.startswith("v1."):
            raise ValueError("Human OAuth credential format is invalid.")
        encrypted = base64.b64decode(ciphertext[3:], altchars=b"-_", validate=True)
        payload = json.loads(
            self._cipher.decrypt(
                encrypted[:12], encrypted[12:], self._binding(session_id, github_id)
            )
        )
        return GitHubOAuthCredential(
            access_token=payload["access_token"],
            access_expires_at=(
                datetime.fromisoformat(payload["access_expires_at"])
                if payload["access_expires_at"]
                else None
            ),
            refresh_token=payload["refresh_token"],
            refresh_expires_at=(
                datetime.fromisoformat(payload["refresh_expires_at"])
                if payload["refresh_expires_at"]
                else None
            ),
        )
