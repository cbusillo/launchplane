from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from control_plane.service_auth import GitHubHumanIdentity
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
    build_browser_mutation_request_headers,
    validate_browser_mutation_request_headers,
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

    def test_csrf_token_rejects_other_session_and_stale_generation(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store)
        first_session = manager.issue(_identity())
        second_session = manager.issue(_identity())
        first_token = manager.csrf_token(first_session)

        self.assertIsNone(manager.consume_csrf_token(second_session, first_token))
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
