from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import os
import secrets
import warnings
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from authlib.integrations.requests_client import (  # type: ignore[import-untyped]
        OAuth2Session as OAuth2SessionType,
    )
else:
    OAuth2SessionType = Any

from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_ORGS_URL = "https://api.github.com/user/orgs"
GITHUB_TEAMS_URL = "https://api.github.com/user/teams"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"
SESSION_COOKIE_NAME = "launchplane_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
OAUTH_STATE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    client_secret: str
    public_url: str
    session_secret: str
    cookie_secure: bool = True
    scopes: tuple[str, ...] = ("read:user", "read:org", "user:email")
    bootstrap_admin_emails: frozenset[str] = frozenset()

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/github/callback"


@dataclass(frozen=True)
class OAuthLoginState:
    state: str
    code_verifier: str
    return_to: str
    expires_at: datetime


@dataclass(frozen=True)
class LaunchplaneHumanSession:
    session_id: str
    identity: GitHubHumanIdentity
    created_at: datetime
    expires_at: datetime


class HumanSessionStore(Protocol):
    def write_session(self, session: LaunchplaneHumanSession) -> None: ...

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None: ...

    def delete_session(self, session_id: str) -> None: ...


class InMemoryHumanSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, LaunchplaneHumanSession] = {}

    def write_session(self, session: LaunchplaneHumanSession) -> None:
        self._sessions[session.session_id] = session

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= datetime.now(timezone.utc):
            self._sessions.pop(session_id, None)
            return None
        return session

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class OAuthLoginStateStore:
    def __init__(self) -> None:
        self._states: dict[str, OAuthLoginState] = {}

    def put(self, *, state: str, code_verifier: str, return_to: str) -> OAuthLoginState:
        login_state = OAuthLoginState(
            state=state,
            code_verifier=code_verifier,
            return_to=return_to or "/",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=OAUTH_STATE_TTL_SECONDS),
        )
        self._states[state] = login_state
        return login_state

    def pop(self, state: str) -> OAuthLoginState | None:
        login_state = self._states.pop(state, None)
        if login_state is None:
            return None
        if login_state.expires_at <= datetime.now(timezone.utc):
            return None
        return login_state


def load_github_oauth_config_from_env() -> GitHubOAuthConfig | None:
    client_id = os.environ.get("LAUNCHPLANE_GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LAUNCHPLANE_GITHUB_CLIENT_SECRET", "").strip()
    public_url = os.environ.get("LAUNCHPLANE_PUBLIC_URL", "").strip().rstrip("/")
    session_secret = os.environ.get("LAUNCHPLANE_SESSION_SECRET", "").strip()
    if not (client_id and client_secret and public_url and session_secret):
        return None
    secure_env = os.environ.get("LAUNCHPLANE_COOKIE_SECURE", "").strip().lower()
    cookie_secure = secure_env not in {"0", "false", "no"}
    bootstrap_admin_emails = frozenset(
        email.lower()
        for email in _split_env_values(os.environ.get("LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS", ""))
    )
    return GitHubOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        public_url=public_url,
        session_secret=session_secret,
        cookie_secure=cookie_secure,
        bootstrap_admin_emails=bootstrap_admin_emails,
    )


def build_pkce_verifier() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class GitHubOAuthClient:
    def __init__(self, config: GitHubOAuthConfig) -> None:
        self._config = config

    @staticmethod
    def _new_session(
        *, client_id: str, client_secret: str, scope: str, redirect_uri: str
    ) -> OAuth2SessionType:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from authlib.integrations.requests_client import OAuth2Session

        return OAuth2Session(
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            redirect_uri=redirect_uri,
        )

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        client = self._new_session(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        authorization_url, _ = client.create_authorization_url(
            GITHUB_AUTHORIZE_URL,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return str(authorization_url)

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        client = self._new_session(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        client.fetch_token(GITHUB_TOKEN_URL, code=code, code_verifier=code_verifier)
        user_payload = client.get(GITHUB_USER_URL).json()
        org_payload = client.get(GITHUB_ORGS_URL).json()
        team_payload = client.get(GITHUB_TEAMS_URL).json()
        email_payload = client.get(GITHUB_EMAILS_URL).json()
        login = str(user_payload.get("login", "")).strip()
        if not login:
            raise ValueError("GitHub OAuth user response did not include a login.")
        public_email = str(user_payload.get("email") or "").strip()
        verified_emails = _verified_email_addresses(email_payload)
        primary_email = _primary_email_address(email_payload)
        email_candidates = {email.lower() for email in verified_emails}
        if public_email:
            email_candidates.add(public_email.lower())
        organizations = frozenset(
            str(org.get("login", "")).strip()
            for org in org_payload
            if isinstance(org, dict) and str(org.get("login", "")).strip()
        )
        teams = frozenset(_team_names(team_payload))
        if self._config.bootstrap_admin_emails.intersection(email_candidates):
            role: Literal["read_only", "admin"] | None = "admin"
        else:
            role = authz_policy.human_role_for(
                login=login,
                organizations=organizations,
                teams=teams,
            )
        if role is None:
            raise PermissionError("GitHub user is not authorized for Launchplane.")
        return GitHubHumanIdentity(
            login=login,
            github_id=int(user_payload.get("id") or 0),
            name=str(user_payload.get("name") or "").strip(),
            email=primary_email or public_email or next(iter(verified_emails), ""),
            organizations=organizations,
            teams=teams,
            role=role,
        )


def _split_env_values(raw_value: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in raw_value.split(",") if value.strip())


def _verified_email_addresses(email_payload: object) -> tuple[str, ...]:
    if not isinstance(email_payload, list):
        return ()
    emails: list[str] = []
    for item in email_payload:
        if not isinstance(item, dict) or item.get("verified") is not True:
            continue
        email = str(item.get("email") or "").strip()
        if email:
            emails.append(email)
    return tuple(emails)


def _primary_email_address(email_payload: object) -> str:
    if not isinstance(email_payload, list):
        return ""
    for item in email_payload:
        if not isinstance(item, dict):
            continue
        if item.get("primary") is True and item.get("verified") is True:
            return str(item.get("email") or "").strip()
    return ""


def _team_names(team_payload: object) -> tuple[str, ...]:
    if not isinstance(team_payload, list):
        return ()
    names: list[str] = []
    for team in team_payload:
        if not isinstance(team, dict):
            continue
        slug = str(team.get("slug") or "").strip()
        organization = team.get("organization")
        org_login = ""
        if isinstance(organization, dict):
            org_login = str(organization.get("login") or "").strip()
        if slug:
            names.append(slug)
        if slug and org_login:
            names.append(f"{org_login}/{slug}")
    return tuple(names)


class HumanSessionManager:
    def __init__(
        self,
        *,
        config: GitHubOAuthConfig,
        session_store: HumanSessionStore,
        now: CallableNow | None = None,
    ) -> None:
        self._config = config
        self._session_store = session_store
        self._now = now or _utc_now

    def issue(self, identity: GitHubHumanIdentity) -> LaunchplaneHumanSession:
        now = self._now()
        session = LaunchplaneHumanSession(
            session_id=secrets.token_urlsafe(32),
            identity=identity,
            created_at=now,
            expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
        )
        self._session_store.write_session(session)
        return session

    def read_cookie(self, cookie_header: str) -> LaunchplaneHumanSession | None:
        signed_session_id = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
        if not signed_session_id:
            return None
        session_id = self._verify_cookie_value(signed_session_id)
        if not session_id:
            return None
        return self._session_store.read_session(session_id)

    def delete_cookie_session(self, cookie_header: str) -> None:
        signed_session_id = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
        if not signed_session_id:
            return
        session_id = self._verify_cookie_value(signed_session_id)
        if session_id:
            self._session_store.delete_session(session_id)

    def session_cookie_header(self, session: LaunchplaneHumanSession) -> str:
        return _build_cookie_header(
            name=SESSION_COOKIE_NAME,
            value=self._sign_cookie_value(session.session_id),
            max_age=SESSION_TTL_SECONDS,
            secure=self._config.cookie_secure,
        )

    def clear_cookie_header(self) -> str:
        return _build_cookie_header(
            name=SESSION_COOKIE_NAME,
            value="",
            max_age=0,
            secure=self._config.cookie_secure,
        )

    def _sign_cookie_value(self, session_id: str) -> str:
        normalized_session_id = session_id.strip()
        signature = hmac.new(
            self._config.session_secret.encode("utf-8"),
            normalized_session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{normalized_session_id}.{signature}"

    def _verify_cookie_value(self, cookie_session_id: str) -> str:
        session_id, separator, signature = cookie_session_id.strip().partition(".")
        if not separator or not signature:
            return ""
        if not session_id or any(character.isspace() for character in session_id):
            return ""
        expected_cookie_value = self._sign_cookie_value(session_id)
        _expected_session_id, _separator, expected_signature = expected_cookie_value.partition(".")
        if not hmac.compare_digest(signature, expected_signature):
            return ""
        return session_id


CallableNow = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in cookie_header.split(";"):
        cookie_name, separator, cookie_value = part.strip().partition("=")
        if separator and cookie_name == name:
            return cookie_value.strip()
    return ""


def _build_cookie_header(*, name: str, value: str, max_age: int, secure: bool) -> str:
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)
