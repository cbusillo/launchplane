from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GITHUB_EMAILS_URL,
    GITHUB_ORGS_URL,
    GITHUB_TEAMS_URL,
    GITHUB_USER_URL,
    GitHubOAuthClient,
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
    build_browser_mutation_request_headers,
    validate_browser_mutation_request_headers,
)


class _FakeGitHubResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeOAuthSession:
    def __init__(self, payloads: dict[str, object]) -> None:
        self._payloads = payloads
        self.token_fetched = False

    def fetch_token(self, *_args: object, **_kwargs: object) -> None:
        self.token_fetched = True

    def get(self, url: str) -> _FakeGitHubResponse:
        return _FakeGitHubResponse(self._payloads[url])


def _config(*, session_secret: str = "session-secret") -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        public_url="https://launchplane.example",
        session_secret=session_secret,
        cookie_secure=False,
    )


def _identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=123,
        name="Alice Example",
        email="alice@example.com",
        organizations=frozenset({"cbusillo"}),
        teams=frozenset({"cbusillo/platform"}),
        role="read_only",
    )


def _oauth_config() -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        public_url="https://launchplane.example",
        session_secret="session-secret",
        bootstrap_admin_emails=frozenset({"alice@example.com"}),
    )


def _oauth_session() -> _FakeOAuthSession:
    return _FakeOAuthSession(
        {
            GITHUB_USER_URL: {
                "login": "alice",
                "id": 123,
                "name": "Alice Example",
                "email": "alice@example.com",
            },
            GITHUB_ORGS_URL: [],
            GITHUB_TEAMS_URL: [],
            GITHUB_EMAILS_URL: [],
        }
    )


class HumanSessionManagerTests(unittest.TestCase):
    def test_bootstrap_admin_role_applies_before_db_human_policy_exists(self) -> None:
        manager = HumanSessionManager(
            config=GitHubOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                public_url="https://launchplane.example",
                session_secret="session-secret",
                bootstrap_admin_emails=frozenset({"alice@example.com"}),
            ),
            session_store=InMemoryHumanSessionStore(),
        )

        self.assertEqual(
            manager.authorized_role(
                identity=_identity(),
                authz_policy=LaunchplaneAuthzPolicy(),
            ),
            "admin",
        )

    def test_bootstrap_admin_role_stops_after_db_human_policy_exists(self) -> None:
        manager = HumanSessionManager(
            config=GitHubOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                public_url="https://launchplane.example",
                session_secret="session-secret",
                bootstrap_admin_emails=frozenset({"alice@example.com"}),
            ),
            session_store=InMemoryHumanSessionStore(),
        )
        policy = LaunchplaneAuthzPolicy(
            github_humans=(
                GitHubHumanPolicyRule(
                    github_ids=(999,),
                    roles=("admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            )
        )

        self.assertIsNone(manager.authorized_role(identity=_identity(), authz_policy=policy))

    def test_legacy_human_policy_does_not_disable_bootstrap_admin(self) -> None:
        manager = HumanSessionManager(
            config=GitHubOAuthConfig(
                client_id="client-id",
                client_secret="client-secret",
                public_url="https://launchplane.example",
                session_secret="session-secret",
                bootstrap_admin_emails=frozenset({"alice@example.com"}),
            ),
            session_store=InMemoryHumanSessionStore(),
        )
        policy = LaunchplaneAuthzPolicy(
            github_humans=(
                GitHubHumanPolicyRule(
                    logins=("*",),
                    actions=("authz_policy_grant.write",),
                ),
            )
        )

        self.assertEqual(
            manager.authorized_role(identity=_identity(), authz_policy=policy), "admin"
        )

    def test_oauth_legacy_human_policy_preserves_bootstrap_admin(self) -> None:
        oauth_session = _oauth_session()
        policy = LaunchplaneAuthzPolicy(
            github_humans=(
                GitHubHumanPolicyRule(
                    logins=("*",),
                    actions=("authz_policy_grant.write",),
                ),
            )
        )

        with patch.object(GitHubOAuthClient, "_new_session", return_value=oauth_session):
            identity = GitHubOAuthClient(_oauth_config()).fetch_identity(
                code="github-code",
                code_verifier="verifier",
                authz_policy=policy,
            )

        self.assertEqual(identity.role, "admin")

    def test_oauth_db_policy_admin_retires_bootstrap_admin(self) -> None:
        oauth_session = _oauth_session()
        policy = LaunchplaneAuthzPolicy(
            github_humans=(
                GitHubHumanPolicyRule(
                    github_ids=(999,),
                    roles=("admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            )
        )

        with (
            patch.object(GitHubOAuthClient, "_new_session", return_value=oauth_session),
            self.assertRaises(PermissionError),
        ):
            GitHubOAuthClient(_oauth_config()).fetch_identity(
                code="github-code",
                code_verifier="verifier",
                authz_policy=policy,
            )

    def test_session_cookie_is_signed_and_round_trips(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        session = manager.issue(_identity())
        cookie = manager.session_cookie_header(session)
        signed_value = cookie.split("launchplane_session=", 1)[1].split(";", 1)[0]

        self.assertIn(f"{session.session_id}.", signed_value)
        self.assertNotEqual(signed_value, session.session_id)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        loaded_session = manager.read_cookie(cookie)
        self.assertIsNotNone(loaded_session)
        assert loaded_session is not None
        self.assertEqual(loaded_session.session_id, session.session_id)

    def test_session_cookie_rejects_tampered_signature(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        session = manager.issue(_identity())
        cookie = manager.session_cookie_header(session)
        tampered_cookie = cookie.replace(session.session_id, f"{session.session_id}-tampered")

        self.assertIsNone(manager.read_cookie(tampered_cookie))

    def test_session_cookie_rejects_signature_from_different_secret(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(
            config=_config(session_secret="first-secret"), session_store=store
        )
        other_manager = HumanSessionManager(
            config=_config(session_secret="second-secret"),
            session_store=store,
        )
        session = manager.issue(_identity())
        cookie = manager.session_cookie_header(session)

        self.assertIsNone(other_manager.read_cookie(cookie))

    def test_session_cookie_rejects_unsigned_or_malformed_values(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        session = manager.issue(_identity())

        self.assertIsNone(manager.read_cookie(f"launchplane_session={session.session_id}"))
        self.assertIsNone(manager.read_cookie("launchplane_session=bad value.signature"))
        self.assertIsNone(manager.read_cookie(f"launchplane_session={session.session_id}.é"))
        self.assertIsNone(manager.read_cookie("other=value"))

    def test_delete_cookie_session_requires_valid_signature(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        session = manager.issue(_identity())

        manager.delete_cookie_session(f"launchplane_session={session.session_id}.bad")

        self.assertIsNotNone(store.read_session(session.session_id))

        manager.delete_cookie_session(manager.session_cookie_header(session))

        self.assertIsNone(store.read_session(session.session_id))

    def test_expired_session_is_removed_after_signed_cookie_read(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        expired_session = LaunchplaneHumanSession(
            session_id="expired-session",
            identity=_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        store.write_session(expired_session)
        cookie = manager.session_cookie_header(expired_session)

        self.assertIsNone(manager.read_cookie(cookie))
        self.assertIsNone(store.read_session(expired_session.session_id))

    def test_authorization_claims_expire_without_extending_session(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        manager = HumanSessionManager(
            config=_config(),
            session_store=InMemoryHumanSessionStore(),
            now=lambda: now,
        )
        current_session = LaunchplaneHumanSession(
            session_id="current-claims",
            identity=_identity(),
            created_at=now - timedelta(hours=23),
            expires_at=now + timedelta(days=13),
        )
        stale_session = LaunchplaneHumanSession(
            session_id="stale-claims",
            identity=_identity(),
            created_at=now - timedelta(hours=24),
            expires_at=now + timedelta(days=13),
        )

        self.assertTrue(manager.authorization_claims_are_current(current_session))
        self.assertFalse(manager.authorization_claims_are_current(stale_session))

    def test_csrf_token_is_session_bound_single_use_and_rotates(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        session = manager.issue(_identity())
        csrf_token = manager.csrf_token(session)

        rotated_session = manager.consume_csrf_token(session, csrf_token)

        self.assertIsNotNone(rotated_session)
        assert rotated_session is not None
        self.assertEqual(rotated_session.csrf_generation, 1)
        self.assertNotEqual(manager.csrf_token(rotated_session), csrf_token)
        self.assertIsNone(manager.consume_csrf_token(session, csrf_token))

    def test_nonpersisting_csrf_validation_does_not_rotate_or_renew_session(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(
            config=_config(),
            session_store=store,
            now=lambda: now,
        )
        session = LaunchplaneHumanSession(
            session_id="nonpersisting-read",
            identity=_identity(),
            created_at=now - timedelta(days=13),
            expires_at=now + timedelta(hours=1),
        )
        store.write_session(session)
        token = manager.csrf_token(session)

        read_session = manager.read_cookie_without_renewal(manager.session_cookie_header(session))

        self.assertEqual(read_session, session)
        assert read_session is not None
        self.assertTrue(manager.csrf_token_is_valid(read_session, token))
        self.assertEqual(store.read_session(session.session_id), session)
        stored_session = store.read_session(session.session_id)
        assert stored_session is not None
        self.assertEqual(stored_session.csrf_generation, 0)

    def test_nonpersisting_session_read_does_not_delete_expired_session(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(
            config=_config(),
            session_store=store,
            now=lambda: now,
        )
        expired_session = LaunchplaneHumanSession(
            session_id="expired-nonpersisting-read",
            identity=_identity(),
            created_at=now - timedelta(days=15),
            expires_at=now - timedelta(seconds=1),
        )
        store.write_session(expired_session)

        resolved_session = manager.read_cookie_without_renewal(
            manager.session_cookie_header(expired_session)
        )

        self.assertIsNone(resolved_session)
        self.assertEqual(
            store.read_session_without_cleanup(expired_session.session_id),
            expired_session,
        )

    def test_csrf_token_rejects_other_session_and_stale_generation(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        first_session = manager.issue(_identity())
        second_session = manager.issue(_identity())
        first_token = manager.csrf_token(first_session)

        self.assertIsNone(manager.consume_csrf_token(second_session, first_token))
        self.assertIsNone(manager.consume_csrf_token(first_session, "v1.0.é"))
        rotated_session = manager.consume_csrf_token(first_session, first_token)
        assert rotated_session is not None
        self.assertIsNone(manager.consume_csrf_token(rotated_session, first_token))
        self.assertIsNone(
            manager.consume_csrf_token(
                rotated_session,
                f"v1.{'9' * 5000}.signature",
            )
        )

    def test_session_renewal_cannot_roll_back_concurrent_csrf_rotation(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(
            config=_config(),
            session_store=store,
            now=lambda: now,
        )
        stale_session = LaunchplaneHumanSession(
            session_id="renewal-csrf-race",
            identity=_identity(),
            created_at=now - timedelta(days=13),
            expires_at=now + timedelta(hours=1),
        )
        store.write_session(stale_session)
        token = manager.csrf_token(stale_session)
        rotated_session = manager.consume_csrf_token(stale_session, token)

        renewed_session = manager.renew_if_needed(stale_session)
        stored_session = store.read_session(stale_session.session_id)

        self.assertIsNotNone(rotated_session)
        self.assertIsNotNone(renewed_session)
        self.assertIsNotNone(stored_session)
        assert renewed_session is not None
        assert stored_session is not None
        self.assertEqual(renewed_session.csrf_generation, 1)
        self.assertEqual(stored_session.csrf_generation, 1)
        self.assertIsNone(manager.consume_csrf_token(stored_session, token))

    def test_browser_mutation_headers_require_exact_same_origin_fetch_metadata(self) -> None:
        manager = HumanSessionManager(config=_config(), session_store=InMemoryHumanSessionStore())
        headers = build_browser_mutation_request_headers(
            origin="https://launchplane.example/operator",
            csrf_token="csrf-token",
        )

        csrf_token = validate_browser_mutation_request_headers(
            expected_origin=manager.public_origin,
            origin_values=(headers["Origin"],),
            sec_fetch_site_values=(headers["Sec-Fetch-Site"],),
            sec_fetch_mode_values=(headers["Sec-Fetch-Mode"],),
            sec_fetch_dest_values=(headers["Sec-Fetch-Dest"],),
            csrf_token_values=(headers["X-CSRF-Token"],),
        )

        self.assertEqual(csrf_token, "csrf-token")
        for overrides in (
            {"origin_values": ()},
            {"origin_values": ("https://attacker.example",)},
            {"sec_fetch_site_values": ("cross-site",)},
            {"sec_fetch_mode_values": ("navigate",)},
            {"sec_fetch_dest_values": ("document",)},
            {"csrf_token_values": ()},
            {"csrf_token_values": ("one", "two")},
        ):
            values: dict[str, tuple[str, ...]] = {
                "origin_values": (headers["Origin"],),
                "sec_fetch_site_values": (headers["Sec-Fetch-Site"],),
                "sec_fetch_mode_values": (headers["Sec-Fetch-Mode"],),
                "sec_fetch_dest_values": (headers["Sec-Fetch-Dest"],),
                "csrf_token_values": (headers["X-CSRF-Token"],),
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(PermissionError):
                validate_browser_mutation_request_headers(
                    expected_origin=manager.public_origin,
                    **values,
                )


if __name__ == "__main__":
    unittest.main()
