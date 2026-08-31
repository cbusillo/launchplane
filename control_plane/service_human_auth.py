from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
from importlib import import_module
import os
import secrets
from threading import RLock
import warnings
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy


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
OAUTH_STATE_TTL_SECONDS = 10 * 60
BROWSER_CSRF_HEADER_NAME = "X-CSRF-Token"
_BROWSER_CSRF_TOKEN_VERSION = "v1"


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


class HumanSessionStore(Protocol):
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
    def json(self) -> Any: ...


class OAuth2SessionType(Protocol):
    def create_authorization_url(self, url: str, **kwargs: object) -> tuple[str, str]: ...

    def fetch_token(self, url: str, **kwargs: object) -> object: ...

    def get(self, url: str) -> OAuthResponse: ...


class InMemoryHumanSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, LaunchplaneHumanSession] = {}
        self._lock = RLock()

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
            self.write_session(human_session)
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
        github_id = int(user_payload.get("id") or 0)
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
        role = authz_policy.human_role_for(
            github_id=github_id,
            login=login,
            organizations=organizations,
            teams=teams,
        )
        bootstrap_admin_email = ""
        if role is None and not _has_db_backed_human_policy_administrator(authz_policy):
            bootstrap_admin_email = next(
                iter(sorted(self._config.bootstrap_admin_emails.intersection(email_candidates))),
                "",
            )
            if bootstrap_admin_email:
                role = "admin"
        if role is None:
            raise PermissionError("GitHub user is not authorized for Launchplane.")
        return GitHubHumanIdentity(
            login=login,
            github_id=github_id,
            name=str(user_payload.get("name") or "").strip(),
            email=(
                bootstrap_admin_email
                or primary_email
                or public_email
                or next(iter(verified_emails), "")
            ),
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


def _has_db_backed_human_policy_administrator(policy: LaunchplaneAuthzPolicy) -> bool:
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
        if role is not None or _has_db_backed_human_policy_administrator(authz_policy):
            return role
        email = identity.email.strip().lower()
        if email and email in self._config.bootstrap_admin_emails:
            return "admin"
        return None

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

    def read_cookie_without_renewal(self, cookie_header: str) -> LaunchplaneHumanSession | None:
        signed_session_id = _cookie_value(cookie_header, SESSION_COOKIE_NAME)
        if not signed_session_id:
            return None
        session_id = self._verify_cookie_value(signed_session_id)
        if not session_id:
            return None
        session = self._session_store.read_session_without_cleanup(session_id)
        if session is None or session.expires_at <= self._now():
            return None
        return session

    def authorization_claims_are_current(self, session: LaunchplaneHumanSession) -> bool:
        now = self._now()
        return (
            session.created_at
            <= now
            < session.created_at + timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS)
        )

    def revoke(self, session: LaunchplaneHumanSession) -> None:
        self._session_store.delete_session(session.session_id)

    def renew_if_needed(self, session: LaunchplaneHumanSession) -> LaunchplaneHumanSession | None:
        now = self._now()
        if session.expires_at <= now:
            self._session_store.delete_session(session.session_id)
            return None
        if session.expires_at - now > timedelta(
            seconds=SESSION_TTL_SECONDS - SESSION_RENEW_AFTER_SECONDS
        ):
            return session
        renewed_session = LaunchplaneHumanSession(
            session_id=session.session_id,
            identity=session.identity,
            created_at=session.created_at,
            expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
            csrf_generation=session.csrf_generation,
        )
        if self._session_store.write_session_if_csrf_generation(
            renewed_session,
            expected_generation=session.csrf_generation,
        ):
            return renewed_session
        current_session = self._session_store.read_session(session.session_id)
        if current_session is None or current_session.expires_at <= now:
            return None
        return current_session

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
