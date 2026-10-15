from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
from importlib import import_module
import os
import logging
import secrets
from threading import RLock
from time import monotonic
import warnings
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_credentials import GitHubOAuthCredential, HumanCredentialCipher


GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_ORGS_URL = "https://api.github.com/user/orgs"
GITHUB_TEAMS_URL = "https://api.github.com/user/teams"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"
SESSION_COOKIE_NAME = "launchplane_session"
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60
SESSION_RENEW_AFTER_SECONDS = 24 * 60 * 60
SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS = 24 * 60 * 60
SESSION_CLAIMS_REFRESH_AFTER_SECONDS = SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS - 60 * 60
OAUTH_REFRESH_MARGIN_SECONDS = 5 * 60
OAUTH_REQUEST_TIMEOUT_SECONDS = 10
OAUTH_EXCHANGE_BUDGET_SECONDS = 30
OAUTH_STATE_TTL_SECONDS = 10 * 60
BROWSER_CSRF_HEADER_NAME = "X-CSRF-Token"
_BROWSER_CSRF_TOKEN_VERSION = "v1"
_LOGGER = logging.getLogger(__name__)


class GitHubIdentityUnavailable(RuntimeError):
    """A transient provider failure, without provider response contents."""


class HumanSessionUpdateBusy(RuntimeError):
    """Another worker is updating this session; no provider exchange was started."""


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
    csrf_generation: int = 0
    claims_verified_at: datetime | None = None
    oauth_credential: str = field(default="", repr=False)


@dataclass(frozen=True)
class GitHubOAuthLogin:
    identity: GitHubHumanIdentity
    credential: GitHubOAuthCredential = field(repr=False)


class HumanSessionStore(Protocol):
    def delete_expired_sessions(self, now: datetime) -> None: ...

    def update_session(
        self,
        session_id: str,
        update: Callable[[LaunchplaneHumanSession], LaunchplaneHumanSession | None],
        *,
        skip_locked: bool = False,
    ) -> LaunchplaneHumanSession | None: ...

    def write_session(self, session: LaunchplaneHumanSession) -> None: ...

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None: ...

    def read_session_without_cleanup(
        self,
        session_id: str,
    ) -> LaunchplaneHumanSession | None: ...

    def delete_session(self, session_id: str) -> None: ...

    def write_session_if_csrf_generation(
        self,
        human_session: LaunchplaneHumanSession,
        *,
        expected_generation: int,
    ) -> bool: ...


class OAuthResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class OAuth2SessionType(Protocol):
    def create_authorization_url(self, url: str, **kwargs: object) -> tuple[str, str]: ...

    def fetch_token(self, url: str, **kwargs: object) -> object: ...

    def get(self, url: str, **kwargs: object) -> OAuthResponse: ...

    def refresh_token(self, url: str, **kwargs: object) -> object: ...

    def close(self) -> None: ...

    token: dict[str, Any]


class GitHubOAuthLoginClient(Protocol):
    def authorization_url(
        self, *, state: str, code_challenge: str, reauthenticate: bool = False
    ) -> str: ...

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
        is_product_owner: Callable[[int], bool] | None = None,
    ) -> GitHubOAuthLogin: ...

    def revalidate(
        self, credential: GitHubOAuthCredential, *, authz_policy: LaunchplaneAuthzPolicy
    ) -> GitHubOAuthLogin: ...


class InMemoryHumanSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, LaunchplaneHumanSession] = {}
        self._lock = RLock()

    def delete_expired_sessions(self, now: datetime) -> None:
        with self._lock:
            self._sessions = {
                key: value for key, value in self._sessions.items() if value.expires_at > now
            }

    def update_session(
        self,
        session_id: str,
        update: Callable[[LaunchplaneHumanSession], LaunchplaneHumanSession | None],
        *,
        skip_locked: bool = False,
    ) -> LaunchplaneHumanSession | None:
        if not self._lock.acquire(blocking=not skip_locked):
            raise HumanSessionUpdateBusy()
        try:
            current = self.read_session(session_id)
            if current is None:
                return None
            updated = update(current)
            if updated is None:
                self._sessions.pop(session_id, None)
            else:
                self._sessions[session_id] = updated
            return updated
        finally:
            self._lock.release()

    def write_session(self, session: LaunchplaneHumanSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= datetime.now(timezone.utc):
                self._sessions.pop(session_id, None)
                return None
            return session

    def read_session_without_cleanup(
        self,
        session_id: str,
    ) -> LaunchplaneHumanSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def write_session_if_csrf_generation(
        self,
        human_session: LaunchplaneHumanSession,
        *,
        expected_generation: int,
    ) -> bool:
        with self._lock:
            current_session = self.read_session(human_session.session_id)
            if current_session is None or current_session.csrf_generation != expected_generation:
                return False
            self.write_session(
                replace(current_session, csrf_generation=human_session.csrf_generation)
            )
            return True


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


LAUNCHPLANE_PUBLIC_URL_ENV_KEY = "LAUNCHPLANE_PUBLIC_URL"


def launchplane_public_origin_from_env() -> str:
    """Launchplane's browser origin from its bootstrap config, or "" when unset or invalid.

    The same `LAUNCHPLANE_PUBLIC_URL` the human session manager's `public_origin` comes from,
    for processes (such as the workers) that build browser links without a session manager.
    """
    public_url = os.environ.get(LAUNCHPLANE_PUBLIC_URL_ENV_KEY, "").strip()
    if not public_url:
        return ""
    try:
        return browser_origin_from_url(public_url)
    except ValueError:
        return ""


def load_github_oauth_config_from_env() -> GitHubOAuthConfig | None:
    client_id = os.environ.get("LAUNCHPLANE_GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LAUNCHPLANE_GITHUB_CLIENT_SECRET", "").strip()
    public_url = os.environ.get(LAUNCHPLANE_PUBLIC_URL_ENV_KEY, "").strip().rstrip("/")
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


def safe_oauth_return_to(value: str) -> str:
    normalized = value.strip() or "/"
    if not normalized.startswith("/") or normalized.startswith("//"):
        return "/"
    return normalized


class GitHubOAuthClient:
    def __init__(self, config: GitHubOAuthConfig) -> None:
        self._config = config

    @staticmethod
    def _new_session(
        *, client_id: str, client_secret: str, scope: str, redirect_uri: str
    ) -> OAuth2SessionType:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            requests_client = import_module("authlib.integrations.requests_client")

        return cast(
            OAuth2SessionType,
            requests_client.OAuth2Session(
                client_id=client_id,
                client_secret=client_secret,
                scope=scope,
                redirect_uri=redirect_uri,
            ),
        )

    def authorization_url(
        self, *, state: str, code_challenge: str, reauthenticate: bool = False
    ) -> str:
        client = self._new_session(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        authorization_arguments: dict[str, object] = {
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if reauthenticate:
            authorization_arguments["prompt"] = "login"
        authorization_url, _ = client.create_authorization_url(
            GITHUB_AUTHORIZE_URL,
            **authorization_arguments,
        )
        return str(authorization_url)

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
        is_product_owner: Callable[[int], bool] | None = None,
    ) -> GitHubOAuthLogin:
        client = self._new_session(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        deadline = monotonic() + OAUTH_EXCHANGE_BUDGET_SECONDS
        try:
            token = client.fetch_token(
                GITHUB_TOKEN_URL,
                code=code,
                code_verifier=code_verifier,
                timeout=OAUTH_REQUEST_TIMEOUT_SECONDS,
            )
            credential = GitHubOAuthCredential.from_token(token, now=_utc_now())
            identity = self._fetch_identity(
                client,
                authz_policy=authz_policy,
                is_product_owner=is_product_owner,
                deadline=deadline,
            )
            return GitHubOAuthLogin(identity, credential)
        finally:
            client.close()

    def revalidate(
        self, credential: GitHubOAuthCredential, *, authz_policy: LaunchplaneAuthzPolicy
    ) -> GitHubOAuthLogin:
        client = self._new_session(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            scope=" ".join(self._config.scopes),
            redirect_uri=self._config.redirect_uri,
        )
        deadline = monotonic() + OAUTH_EXCHANGE_BUDGET_SECONDS
        try:
            now = _utc_now()
            if credential.access_expires_at is not None and credential.access_expires_at <= (
                now + timedelta(seconds=OAUTH_REFRESH_MARGIN_SECONDS)
            ):
                if (
                    not credential.refresh_token
                    or credential.refresh_expires_at is None
                    or credential.refresh_expires_at <= now
                ):
                    raise PermissionError("GitHub OAuth credential requires sign-in.")
                # GitHub invalidates both old tokens. The session store holds
                # its row lock through refresh and replacement to serialize this.
                token = client.refresh_token(
                    GITHUB_TOKEN_URL,
                    refresh_token=credential.refresh_token,
                    scope=None,
                    timeout=OAUTH_REQUEST_TIMEOUT_SECONDS,
                )
                credential = GitHubOAuthCredential.from_token(token, now=now)
            client.token = {"access_token": credential.access_token, "token_type": "Bearer"}
            identity = self._fetch_identity(client, authz_policy=authz_policy, deadline=deadline)
            return GitHubOAuthLogin(identity, credential)
        finally:
            client.close()

    @staticmethod
    def _get_payload(client: OAuth2SessionType, url: str, *, deadline: float) -> Any:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise GitHubIdentityUnavailable("GitHub verification budget exhausted.")
        response = client.get(
            url, timeout=min(OAUTH_REQUEST_TIMEOUT_SECONDS, remaining), allow_redirects=False
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise GitHubIdentityUnavailable("GitHub identity provider is unavailable.")
        if response.status_code != 200:
            raise PermissionError("GitHub identity could not be verified.")
        return response.json()

    def _list_payload(
        self, client: OAuth2SessionType, url: str, *, deadline: float
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 101):
            payload = self._get_payload(
                client, f"{url}?per_page=100&page={page}", deadline=deadline
            )
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise ValueError("GitHub membership response is invalid.")
            result.extend(payload)
            if len(payload) < 100:
                return result
        raise ValueError("GitHub membership response exceeds its bound.")

    def _fetch_identity(
        self,
        client: OAuth2SessionType,
        *,
        authz_policy: LaunchplaneAuthzPolicy,
        is_product_owner: Callable[[int], bool] | None = None,
        deadline: float,
    ) -> GitHubHumanIdentity:
        user_payload = self._get_payload(client, GITHUB_USER_URL, deadline=deadline)
        if not isinstance(user_payload, dict):
            raise ValueError("GitHub user response is invalid.")
        org_payload = self._list_payload(client, GITHUB_ORGS_URL, deadline=deadline)
        team_payload = self._list_payload(client, GITHUB_TEAMS_URL, deadline=deadline)
        email_payload = self._list_payload(client, GITHUB_EMAILS_URL, deadline=deadline)
        if any(
            not isinstance(org.get("login"), str) or not org["login"].strip() for org in org_payload
        ):
            raise ValueError("GitHub organization response is invalid.")
        if any(
            not isinstance(team.get("slug"), str)
            or not team["slug"].strip()
            or not isinstance(team.get("organization"), dict)
            or not isinstance(team["organization"].get("login"), str)
            or not team["organization"]["login"].strip()
            for team in team_payload
        ):
            raise ValueError("GitHub team response is invalid.")
        if any(
            not isinstance(email.get("email"), str)
            or not email["email"].strip()
            or not isinstance(email.get("verified"), bool)
            for email in email_payload
        ):
            raise ValueError("GitHub email response is invalid.")
        login_value = user_payload.get("login")
        login = login_value.strip() if isinstance(login_value, str) else ""
        if not login:
            raise ValueError("GitHub OAuth user response did not include a login.")
        github_id = user_payload.get("id")
        if isinstance(github_id, bool) or not isinstance(github_id, int) or github_id <= 0:
            raise ValueError("GitHub user response has no immutable identity.")
        public_email = str(user_payload.get("email") or "").strip()
        verified_emails = _verified_email_addresses(email_payload)
        primary_email = _primary_email_address(email_payload)
        email_candidates = {email.lower() for email in verified_emails}
        organizations = frozenset(
            str(org.get("login", "")).strip()
            for org in org_payload
            if isinstance(org, dict) and str(org.get("login", "")).strip()
        )
        teams = frozenset(_team_names(team_payload))
        role: Literal["read_only", "admin", "owner"] | None = authz_policy.human_role_for(
            github_id=github_id,
            login=login,
            organizations=organizations,
            teams=teams,
        )
        bootstrap_admin_email = ""
        if role is None and not _has_db_backed_human_admin(authz_policy):
            bootstrap_admin_email = next(
                iter(sorted(self._config.bootstrap_admin_emails.intersection(email_candidates))),
                "",
            )
            if bootstrap_admin_email:
                role = "admin"
        # A product's named Owner may sign in without a policy role; the session
        # can reach only the Owner review routes (see read_cookie).
        if role is None and is_product_owner is not None and is_product_owner(github_id):
            role = "owner"
        if role is None:
            raise PermissionError("GitHub user is not authorized for Launchplane.")
        return GitHubHumanIdentity(
            login=login,
            github_id=github_id,
            name=str(user_payload.get("name") or "").strip(),
            email=(
                bootstrap_admin_email
                or primary_email
                or (public_email if role == "owner" else "")
                or next(iter(verified_emails), "")
            ),
            organizations=organizations,
            teams=teams,
            role=role,
        )


def _owner_session_filter(
    session: LaunchplaneHumanSession | None, *, allow_owner: bool
) -> LaunchplaneHumanSession | None:
    if session is not None and session.identity.role == "owner" and not allow_owner:
        return None
    return session


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


def _has_db_backed_human_admin(policy: LaunchplaneAuthzPolicy) -> bool:
    return any(
        bool(rule.github_ids)
        and not any((rule.logins, rule.organizations, rule.teams))
        and "admin" in rule.roles
        and rule.products == ("launchplane",)
        and rule.contexts == ("launchplane",)
        and "authz_policy_grant.write" in rule.actions
        for rule in policy.github_humans
    )


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
        self._credential_cipher = HumanCredentialCipher(config.session_secret)

    @property
    def public_origin(self) -> str:
        return browser_origin_from_url(self._config.public_url)

    def authorized_role(
        self,
        *,
        identity: GitHubHumanIdentity,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> Literal["read_only", "admin"] | None:
        role = authz_policy.human_role_for(
            github_id=identity.github_id,
            login=identity.login,
            organizations=identity.organizations,
            teams=identity.teams,
        )
        if role is not None or _has_db_backed_human_admin(authz_policy):
            return role
        email = identity.email.strip().lower()
        if email and email in self._config.bootstrap_admin_emails:
            return "admin"
        return None

    def issue(
        self, identity: GitHubHumanIdentity, credential: GitHubOAuthCredential | None = None
    ) -> LaunchplaneHumanSession:
        now = self._now()
        session = LaunchplaneHumanSession(
            session_id=secrets.token_urlsafe(32),
            identity=identity,
            created_at=now,
            expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
        )
        if credential is not None and identity.role != "owner":
            session = replace(
                session,
                claims_verified_at=now,
                oauth_credential=self._credential_cipher.encrypt(
                    credential, session_id=session.session_id, github_id=identity.github_id
                ),
            )
        self._session_store.delete_expired_sessions(now)
        self._session_store.write_session(session)
        return session

    # Owner sessions are refused unless the caller explicitly accepts them, so
    # every existing session reader stays closed to Owners by default.
    def read_cookie(
        self, cookie_header: str, *, allow_owner: bool = False
    ) -> LaunchplaneHumanSession | None:
        signed_session_id = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
        if not signed_session_id:
            return None
        session_id = self._verify_cookie_value(signed_session_id)
        if not session_id:
            return None
        return _owner_session_filter(
            self._session_store.read_session(session_id), allow_owner=allow_owner
        )

    def read_cookie_without_renewal(
        self, cookie_header: str, *, allow_owner: bool = False
    ) -> LaunchplaneHumanSession | None:
        signed_session_id = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
        if not signed_session_id:
            return None
        session_id = self._verify_cookie_value(signed_session_id)
        if not session_id:
            return None
        session = self._session_store.read_session_without_cleanup(session_id)
        if session is None or session.expires_at <= self._now():
            return None
        return _owner_session_filter(session, allow_owner=allow_owner)

    def authorization_claims_are_current(self, session: LaunchplaneHumanSession) -> bool:
        now = self._now()
        verified_at = session.claims_verified_at or session.created_at
        return (
            verified_at
            <= now
            < verified_at + timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS)
        )

    def revoke(self, session: LaunchplaneHumanSession) -> None:
        self._session_store.delete_session(session.session_id)

    def _refresh_due(self, session: LaunchplaneHumanSession, now: datetime) -> bool:
        if not session.oauth_credential:
            return False
        verified_at = session.claims_verified_at or session.created_at
        if now >= verified_at + timedelta(seconds=SESSION_CLAIMS_REFRESH_AFTER_SECONDS):
            return True
        credential = self._credential_cipher.decrypt(
            session.oauth_credential,
            session_id=session.session_id,
            github_id=session.identity.github_id,
        )
        return credential.access_expires_at is not None and (
            credential.access_expires_at <= now + timedelta(seconds=OAUTH_REFRESH_MARGIN_SECONDS)
        )

    def _fresh_session(
        self, session: LaunchplaneHumanSession | None
    ) -> LaunchplaneHumanSession | None:
        if session is None or session.expires_at <= self._now():
            return None
        return session if self.authorization_claims_are_current(session) else None

    def refresh_authorization_claims(
        self,
        session: LaunchplaneHumanSession,
        *,
        client: GitHubOAuthLoginClient | None,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> LaunchplaneHumanSession | None:
        if session.identity.role == "owner":
            return session
        try:
            if not self._refresh_due(session, self._now()):
                fresh = self._fresh_session(session)
                if fresh is None:
                    self.revoke(session)
                return fresh
        except Exception:
            self.revoke(session)
            return None

        def refresh(current: LaunchplaneHumanSession) -> LaunchplaneHumanSession | None:
            now = self._now()
            verified_at = current.claims_verified_at or current.created_at
            if current.expires_at <= now or verified_at > now:
                return None
            try:
                if not self._refresh_due(current, now):
                    return current
                if client is None:
                    return None
                credential = self._credential_cipher.decrypt(
                    current.oauth_credential,
                    session_id=current.session_id,
                    github_id=current.identity.github_id,
                )
                login = client.revalidate(credential, authz_policy=authz_policy)
                if (
                    login.identity.github_id != current.identity.github_id
                    or login.identity.role == "owner"
                ):
                    return None
                return replace(
                    current,
                    identity=login.identity,
                    claims_verified_at=now,
                    oauth_credential=self._credential_cipher.encrypt(
                        login.credential,
                        session_id=current.session_id,
                        github_id=current.identity.github_id,
                    ),
                )
            except Exception as error:
                # OAuth exception values can contain credentials; log only type.
                _LOGGER.warning(
                    "Human session provider verification failed (%s).", type(error).__name__
                )
                return None

        self._session_store.delete_expired_sessions(self._now())
        try:
            return self._session_store.update_session(session.session_id, refresh, skip_locked=True)
        except HumanSessionUpdateBusy:
            # An exchange is in progress, not failed. A follower may use only
            # already-current claims; it cannot renew them or wait on a DB lock.
            current = self._session_store.read_session_without_cleanup(session.session_id)
            if current is None or current.expires_at <= self._now():
                return None
            fresh = self._fresh_session(current)
            if fresh is None:
                raise HumanSessionUpdateBusy() from None
            return fresh

    def renew_if_needed(self, session: LaunchplaneHumanSession) -> LaunchplaneHumanSession | None:
        now = self._now()
        if session.expires_at - now > timedelta(
            seconds=SESSION_TTL_SECONDS - SESSION_RENEW_AFTER_SECONDS
        ):
            return session

        def renew(current: LaunchplaneHumanSession) -> LaunchplaneHumanSession | None:
            if current.expires_at <= now:
                return None
            if current.expires_at - now > timedelta(
                seconds=SESSION_TTL_SECONDS - SESSION_RENEW_AFTER_SECONDS
            ):
                return current
            return replace(current, expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS))

        try:
            return self._session_store.update_session(
                session.session_id, renew, skip_locked=session.identity.role != "owner"
            )
        except HumanSessionUpdateBusy:
            return self._fresh_session(
                self._session_store.read_session_without_cleanup(session.session_id)
            )

    def csrf_token(self, session: LaunchplaneHumanSession) -> str:
        generation = session.csrf_generation
        if generation < 0:
            raise ValueError("Launchplane human session CSRF generation must be non-negative.")
        signature = self._csrf_signature(
            session_id=session.session_id,
            generation=generation,
        )
        return f"{_BROWSER_CSRF_TOKEN_VERSION}.{generation}.{signature}"

    def csrf_token_is_valid(self, session: LaunchplaneHumanSession, token: str) -> bool:
        normalized_token = token.strip()
        if not normalized_token.isascii():
            return False
        generation = _csrf_token_generation(normalized_token)
        if generation is None or generation != session.csrf_generation:
            return False
        return hmac.compare_digest(normalized_token, self.csrf_token(session))

    def consume_csrf_token(
        self,
        session: LaunchplaneHumanSession,
        token: str,
    ) -> LaunchplaneHumanSession | None:
        if not self.csrf_token_is_valid(session, token):
            return None
        generation = session.csrf_generation
        rotated_session = replace(session, csrf_generation=generation + 1)
        if not self._session_store.write_session_if_csrf_generation(
            rotated_session,
            expected_generation=generation,
        ):
            return None
        return rotated_session

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
            self._config.session_secret.encode(),
            normalized_session_id.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{normalized_session_id}.{signature}"

    def _csrf_signature(self, *, session_id: str, generation: int) -> str:
        signature_payload = b"\0".join(
            (
                b"launchplane-browser-csrf-v1",
                session_id.encode(),
                str(generation).encode(),
            )
        )
        signature = hmac.new(
            self._config.session_secret.encode(),
            signature_payload,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def _verify_cookie_value(self, cookie_session_id: str) -> str:
        session_id, separator, signature = cookie_session_id.strip().partition(".")
        if not separator or not signature:
            return ""
        if (
            not session_id
            or not signature.isascii()
            or any(character.isspace() for character in session_id)
        ):
            return ""
        expected_cookie_value = self._sign_cookie_value(session_id)
        _expected_session_id, _separator, expected_signature = expected_cookie_value.partition(".")
        if not hmac.compare_digest(signature, expected_signature):
            return ""
        return session_id


CallableNow = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def browser_origin_from_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Launchplane browser origin requires an HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Launchplane browser origin must not include userinfo.")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Launchplane browser origin has an invalid port.") from error
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    port_suffix = ""
    if port is not None and port != default_port:
        port_suffix = f":{int(port)}"
    return f"{scheme}://{host}{port_suffix}"


def build_browser_mutation_request_headers(*, origin: str, csrf_token: str) -> dict[str, str]:
    normalized_token = csrf_token.strip()
    if not normalized_token:
        raise ValueError("Launchplane browser mutation CSRF token is required.")
    return {
        "Origin": browser_origin_from_url(origin),
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        BROWSER_CSRF_HEADER_NAME: normalized_token,
    }


def validate_browser_mutation_request_headers(
    *,
    expected_origin: str,
    origin_values: tuple[str, ...],
    sec_fetch_site_values: tuple[str, ...],
    sec_fetch_mode_values: tuple[str, ...],
    sec_fetch_dest_values: tuple[str, ...],
    csrf_token_values: tuple[str, ...],
) -> str:
    if not all(
        len(values) == 1
        for values in (
            origin_values,
            sec_fetch_site_values,
            sec_fetch_mode_values,
            sec_fetch_dest_values,
            csrf_token_values,
        )
    ):
        raise PermissionError("Browser mutation request metadata is invalid.")
    origin = origin_values[0].strip()
    parsed_origin = urlsplit(origin)
    if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
        raise PermissionError("Browser mutation request metadata is invalid.")
    try:
        normalized_origin = browser_origin_from_url(origin)
    except ValueError as error:
        raise PermissionError("Browser mutation request metadata is invalid.") from error
    if normalized_origin != expected_origin:
        raise PermissionError("Browser mutation request metadata is invalid.")
    if sec_fetch_site_values[0].strip().lower() != "same-origin":
        raise PermissionError("Browser mutation request metadata is invalid.")
    if sec_fetch_mode_values[0].strip().lower() not in {"cors", "same-origin"}:
        raise PermissionError("Browser mutation request metadata is invalid.")
    if sec_fetch_dest_values[0].strip().lower() != "empty":
        raise PermissionError("Browser mutation request metadata is invalid.")
    csrf_token = csrf_token_values[0].strip()
    if not csrf_token:
        raise PermissionError("Browser mutation request metadata is invalid.")
    return csrf_token


def validate_browser_sensitive_read_request_headers(
    *,
    expected_origin: str,
    origin_values: tuple[str, ...],
    sec_fetch_site_values: tuple[str, ...],
    sec_fetch_mode_values: tuple[str, ...],
    sec_fetch_dest_values: tuple[str, ...],
    csrf_token_values: tuple[str, ...],
) -> str:
    if len(origin_values) > 1 or not all(
        len(values) == 1
        for values in (
            sec_fetch_site_values,
            sec_fetch_mode_values,
            sec_fetch_dest_values,
            csrf_token_values,
        )
    ):
        raise PermissionError("Browser sensitive read request metadata is invalid.")
    if origin_values:
        origin = origin_values[0].strip()
        parsed_origin = urlsplit(origin)
        if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
            raise PermissionError("Browser sensitive read request metadata is invalid.")
        try:
            normalized_origin = browser_origin_from_url(origin)
        except ValueError as error:
            raise PermissionError("Browser sensitive read request metadata is invalid.") from error
        if normalized_origin != expected_origin:
            raise PermissionError("Browser sensitive read request metadata is invalid.")
    if sec_fetch_site_values[0].strip().lower() != "same-origin":
        raise PermissionError("Browser sensitive read request metadata is invalid.")
    if sec_fetch_mode_values[0].strip().lower() not in {"cors", "same-origin"}:
        raise PermissionError("Browser sensitive read request metadata is invalid.")
    if sec_fetch_dest_values[0].strip().lower() != "empty":
        raise PermissionError("Browser sensitive read request metadata is invalid.")
    csrf_token = csrf_token_values[0].strip()
    if not csrf_token:
        raise PermissionError("Browser sensitive read request metadata is invalid.")
    return csrf_token


def _csrf_token_generation(token: str) -> int | None:
    if len(token) > 256:
        return None
    version, separator, remainder = token.partition(".")
    generation_text, generation_separator, signature = remainder.partition(".")
    if (
        version != _BROWSER_CSRF_TOKEN_VERSION
        or not separator
        or not generation_separator
        or not signature
        or not generation_text.isascii()
        or not generation_text.isdecimal()
        or len(generation_text) > 20
    ):
        return None
    try:
        generation = int(generation_text)
    except ValueError:
        return None
    if generation_text != str(generation):
        return None
    return generation


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
