from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from control_plane.service_auth import GitHubHumanIdentity
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)


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


class HumanSessionManagerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
