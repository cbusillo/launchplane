from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs

from requests import PreparedRequest, Response, Session
from requests.adapters import BaseAdapter
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from cryptography.exceptions import InvalidTag

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    GITHUB_EMAILS_URL,
    GITHUB_ORGS_URL,
    GITHUB_TEAMS_URL,
    GITHUB_USER_URL,
    GitHubOAuthClient,
    GitHubOAuthLogin,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    OAUTH_REFRESH_MARGIN_SECONDS,
    SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS,
    SESSION_CLAIMS_REFRESH_AFTER_SECONDS,
    SESSION_RENEW_AFTER_SECONDS,
    SESSION_TTL_SECONDS,
)
from control_plane.service_human_credentials import GitHubOAuthCredential, HumanCredentialCipher
from control_plane.storage.postgres import _human_session_from_payload, _human_session_payload
from tests.http_app_test_support import _RejectingVerifier, _StubFastApiGitHubOAuthClient
from tests.support.http import lifespan_client
from tests.test_service import _HostedAuthzPolicyStore
from tests.test_service_human_auth import _config, _identity


def _policy(role: str = "admin") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {"github_humans": [{"github_ids": [_identity().github_id], "roles": [role]}]}
    )


class HumanSessionRenewalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.store = InMemoryHumanSessionStore()
        self.manager = HumanSessionManager(
            config=_config(), session_store=self.store, now=lambda: self.now
        )
        self.credential = GitHubOAuthCredential(
            "synthetic-access", refresh_token="synthetic-refresh"
        )
        self.session = self.manager.issue(_identity(), self.credential)
        self.client = Mock(spec=GitHubOAuthClient)
        self.client.revalidate.return_value = GitHubOAuthLogin(_identity(), self.credential)

    def refresh(self) -> Any:
        return self.manager.refresh_authorization_claims(
            self.session, client=self.client, authz_policy=_policy()
        )

    def advance_to_refresh(self) -> None:
        self.now += timedelta(seconds=SESSION_CLAIMS_REFRESH_AFTER_SECONDS)

    def test_credential_is_encrypted_and_bound_to_session_and_person(self) -> None:
        ciphertext = self.session.oauth_credential
        payload = _human_session_payload(self.session)
        self.assertNotIn(self.credential.access_token, json.dumps(payload))
        self.assertNotIn(self.credential.refresh_token, json.dumps(payload))
        cipher = HumanCredentialCipher(_config().session_secret)
        self.assertEqual(
            cipher.decrypt(
                ciphertext,
                session_id=self.session.session_id,
                github_id=self.session.identity.github_id,
            ),
            self.credential,
        )
        for session_id, github_id, root in (
            ("another-session", self.session.identity.github_id, _config().session_secret),
            (
                self.session.session_id,
                self.session.identity.github_id + 1,
                _config().session_secret,
            ),
            (self.session.session_id, self.session.identity.github_id, "rotated-root"),
        ):
            with (
                self.subTest(session_id=session_id, github_id=github_id, root=root),
                self.assertRaises(InvalidTag),
            ):
                HumanCredentialCipher(root).decrypt(
                    ciphertext, session_id=session_id, github_id=github_id
                )
        self.assertEqual(_human_session_from_payload(payload), self.session)
        self.assertNotIn(ciphertext, repr(self.session))
        self.assertNotIn(self.credential.access_token, repr(self.credential))

    def test_background_reads_refresh_before_claims_ceiling_and_reuse_recent_verification(
        self,
    ) -> None:
        self.assertEqual(self.refresh(), self.session)
        self.client.revalidate.assert_not_called()
        self.advance_to_refresh()
        refreshed = self.refresh()
        self.assertIsNotNone(refreshed)
        self.assertLess(
            self.now,
            self.session.created_at + timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS),
        )
        self.assertEqual(refreshed.claims_verified_at, self.now)
        self.assertEqual(refreshed.created_at, self.session.created_at)
        self.assertEqual(refreshed.expires_at, self.session.expires_at)
        self.assertEqual(self.refresh(), refreshed)
        self.client.revalidate.assert_called_once()

    def test_return_after_claims_expire_requires_provider_validation_before_cookie_renewal(
        self,
    ) -> None:
        self.now += timedelta(seconds=SESSION_RENEW_AFTER_SECONDS + 1)
        self.assertFalse(self.manager.authorization_claims_are_current(self.session))
        refreshed = self.refresh()
        self.assertTrue(self.manager.authorization_claims_are_current(refreshed))
        renewed = self.manager.renew_if_needed(refreshed)
        assert renewed is not None
        self.assertEqual(renewed.expires_at, self.now + timedelta(seconds=SESSION_TTL_SECONDS))
        self.assertEqual(renewed.session_id, self.session.session_id)
        self.assertEqual(self.manager.csrf_token(renewed), self.manager.csrf_token(self.session))

    def test_provider_failure_or_changed_identity_revokes_instead_of_extending(self) -> None:
        for failure in (
            OSError("synthetic provider failure"),
            PermissionError("revoked"),
            ValueError("malformed"),
            None,
        ):
            with self.subTest(failure=failure):
                self.setUp()
                self.advance_to_refresh()
                if failure is not None:
                    self.client.revalidate.side_effect = failure
                else:
                    self.client.revalidate.return_value = GitHubOAuthLogin(
                        replace(_identity(), github_id=999), self.credential
                    )
                self.assertIsNone(self.refresh())
                self.assertIsNone(self.store.read_session(self.session.session_id))
                self.assertIsNone(self.manager.renew_if_needed(self.session))

    def test_missing_credentials_never_extend_stale_claims(self) -> None:
        self.session = self.manager.issue(_identity())
        self.now += timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS)
        self.assertIsNone(self.refresh())
        self.client.revalidate.assert_not_called()

    def test_corrupt_ciphertext_fails_closed_without_provider_call(self) -> None:
        self.session = replace(self.session, oauth_credential="v1.broken")
        self.store.write_session(self.session)
        self.assertIsNone(self.refresh())
        self.client.revalidate.assert_not_called()

    def test_expired_or_revoked_session_cannot_be_resurrected(self) -> None:
        self.advance_to_refresh()
        self.manager.revoke(self.session)
        self.assertIsNone(self.refresh())
        self.client.revalidate.assert_not_called()
        self.store.write_session(replace(self.session, expires_at=self.now))
        self.assertIsNone(self.refresh())
        self.client.revalidate.assert_not_called()

    def test_csrf_consumption_preserves_concurrently_refreshed_claims_and_credentials(self) -> None:
        token = self.manager.csrf_token(self.session)
        self.advance_to_refresh()
        refreshed = self.refresh()
        self.assertIsNotNone(self.manager.consume_csrf_token(self.session, token))
        stored = self.store.read_session(self.session.session_id)
        assert stored is not None
        self.assertEqual(stored.oauth_credential, refreshed.oauth_credential)
        self.assertEqual(stored.claims_verified_at, refreshed.claims_verified_at)
        self.assertIsNone(self.manager.consume_csrf_token(self.session, token))
        self.assertEqual(self.refresh().csrf_generation, stored.csrf_generation)

    def test_client_session_stores_no_credential_and_never_refreshes_identity(self) -> None:
        owner = self.manager.issue(replace(_identity(), role="owner"), self.credential)
        self.assertEqual(owner.oauth_credential, "")
        self.now += timedelta(days=2)
        self.assertEqual(
            self.manager.refresh_authorization_claims(
                owner, client=self.client, authz_policy=_policy()
            ),
            owner,
        )
        self.client.revalidate.assert_not_called()


class GitHubCredentialRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.http = Mock()
        payloads = {
            GITHUB_USER_URL: {"id": _identity().github_id, "login": _identity().login},
            GITHUB_ORGS_URL: [],
            GITHUB_TEAMS_URL: [],
            GITHUB_EMAILS_URL: [],
        }
        self.payloads = payloads
        self.http.get.side_effect = lambda url, **kwargs: Mock(
            status_code=200, json=lambda: self.payloads[url.split("?", 1)[0]]
        )
        self.client = GitHubOAuthClient(_config())
        self.http.refresh_token.return_value = {
            "access_token": "new-synthetic-access",
            "refresh_token": "new-synthetic-refresh",
            "token_type": "bearer",
            "expires_in": 28800,
            "refresh_token_expires_in": 15000000,
        }

    def revalidate(self, credential: GitHubOAuthCredential) -> GitHubOAuthLogin:
        with (
            patch.object(self.client, "_new_session", return_value=self.http),
            patch("control_plane.service_human_auth._utc_now", return_value=self.now),
        ):
            return self.client.revalidate(credential, authz_policy=_policy())

    def test_expiring_access_rotates_pair_and_revalidates_immutable_identity(self) -> None:
        credential = GitHubOAuthCredential(
            "old-access", self.now, "old-refresh", self.now + timedelta(days=3)
        )
        result = self.revalidate(credential)
        self.assertEqual(result.identity.github_id, _identity().github_id)
        self.assertEqual(result.credential.access_token, self.http.token["access_token"])
        self.assertNotEqual(result.credential.refresh_token, credential.refresh_token)
        self.http.refresh_token.assert_called_once()
        self.assertIsNone(self.http.refresh_token.call_args.kwargs["scope"])
        self.http.close.assert_called_once()

    def test_long_lived_access_revalidates_without_refreshing_token(self) -> None:
        credential = GitHubOAuthCredential("long-lived-access")
        self.assertEqual(self.revalidate(credential).credential, credential)
        self.http.refresh_token.assert_not_called()
        self.assertEqual(self.http.get.call_count, len(self.payloads))

    def test_expired_refresh_or_refresh_error_refuses_identity(self) -> None:
        for refresh_token, refresh_deadline in (("", None), ("expired-refresh", self.now)):
            with self.subTest(refresh_token=refresh_token), self.assertRaises(PermissionError):
                self.revalidate(
                    GitHubOAuthCredential(
                        "expired-access", self.now, refresh_token, refresh_deadline
                    )
                )
        self.http.refresh_token.assert_not_called()
        self.http.get.assert_not_called()
        self.http.refresh_token.return_value = {"error": "bad_refresh_token"}
        with self.assertRaises(ValueError):
            self.revalidate(
                GitHubOAuthCredential(
                    "old-access", self.now, "refresh", self.now + timedelta(days=2)
                )
            )
        self.http.get.assert_not_called()

    def test_provider_http_errors_and_malformed_memberships_fail_closed(self) -> None:
        for status in (302, 401, 403, 429, 503):
            with self.subTest(status=status):
                self.http.get.side_effect = None
                self.http.get.return_value = Mock(status_code=status)
                with self.assertRaises(PermissionError):
                    self.revalidate(GitHubOAuthCredential("access"))
        self.http.get.side_effect = lambda url, **kwargs: Mock(
            status_code=200,
            json=lambda: {} if GITHUB_ORGS_URL in url else self.payloads[url.split("?", 1)[0]],
        )
        with self.assertRaises(ValueError):
            self.revalidate(GitHubOAuthCredential("access"))

    def test_membership_pagination_reaches_later_policy_match(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"organizations": ["allowed-org"], "roles": ["read_only"]}]}
        )

        def response(url: str, **kwargs: object) -> Mock:
            payload: object = self.payloads[url.split("?", 1)[0]]
            if GITHUB_ORGS_URL in url:
                payload = (
                    [{"login": "other"}] * 100
                    if url.endswith("page=1")
                    else [{"login": "allowed-org"}]
                )
            return Mock(status_code=200, json=lambda: payload)

        self.http.get.side_effect = response
        with patch.object(self.client, "_new_session", return_value=self.http):
            result = self.client.revalidate(GitHubOAuthCredential("access"), authz_policy=policy)
        self.assertIn("allowed-org", result.identity.organizations)

    def test_refresh_uses_authlib_with_github_token_response(self) -> None:
        payloads = self.payloads
        token = self.http.refresh_token.return_value
        requests: list[Any] = []

        class Adapter(BaseAdapter):
            def send(
                self,
                request: PreparedRequest,
                stream: bool = False,
                timeout: float | tuple[float | None, float | None] | None = None,
                verify: bool | str = True,
                cert: str | tuple[str, str] | None = None,
                proxies: dict[str, str] | None = None,
            ) -> Response:
                requests.append(request)
                response = Response()
                response.status_code = 200
                response.request = request
                payload = (
                    token
                    if request.method == "POST"
                    else payloads[str(request.url).split("?", 1)[0]]
                )
                response._content = json.dumps(payload).encode()
                response.headers["Content-Type"] = "application/json"
                return response

            def close(self) -> None:
                pass

        session = self.client._new_session(
            client_id=_config().client_id,
            client_secret=_config().client_secret,
            scope=" ".join(_config().scopes),
            redirect_uri=_config().redirect_uri,
        )
        cast(Session, session).mount("https://", Adapter())
        with patch.object(self.client, "_new_session", return_value=session):
            result = self.client.revalidate(
                GitHubOAuthCredential("old", self.now, "refresh", self.now + timedelta(days=1)),
                authz_policy=_policy(),
            )
        self.assertEqual(result.credential.access_token, token["access_token"])
        self.assertEqual(result.credential.refresh_token, token["refresh_token"])
        assert result.credential.access_expires_at is not None
        self.assertGreater(result.credential.access_expires_at, self.now)
        body = parse_qs(requests[0].body)
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertNotIn("scope", body)
        self.assertTrue(all(request.headers.get("Authorization") for request in requests))

    def test_removed_membership_cannot_renew_the_prior_role(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"organizations": ["formerly-allowed"], "roles": ["admin"]}]}
        )
        with patch.object(self.client, "_new_session", return_value=self.http):
            with self.assertRaises(PermissionError):
                self.client.revalidate(GitHubOAuthCredential("access"), authz_policy=policy)

    def test_malformed_or_error_token_responses_are_not_retained(self) -> None:
        for token in (
            None,
            {"error": "bad_refresh_token"},
            {"access_token": "access"},
            {"access_token": "access", "token_type": "bearer", "expires_in": -1},
            {"access_token": "access", "token_type": "bearer", "refresh_token": "refresh"},
        ):
            with self.subTest(token=token), self.assertRaises(ValueError):
                GitHubOAuthCredential.from_token(token, now=self.now)

    def test_expiring_access_triggers_early_claim_refresh(self) -> None:
        store = InMemoryHumanSessionStore()
        manager = HumanSessionManager(config=_config(), session_store=store, now=lambda: self.now)
        credential = GitHubOAuthCredential(
            "access",
            self.now + timedelta(seconds=OAUTH_REFRESH_MARGIN_SECONDS),
            "refresh",
            self.now + timedelta(days=1),
        )
        session = manager.issue(_identity(), credential)
        with patch.object(self.client, "_new_session", return_value=self.http):
            refreshed = manager.refresh_authorization_claims(
                session, client=self.client, authz_policy=_policy()
            )
        self.assertIsNotNone(refreshed)
        self.http.refresh_token.assert_called_once()


class HostedRenewalHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_renewal_uses_current_db_policy_and_never_exposes_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = _HostedAuthzPolicyStore(
                database_url=f"sqlite+pysqlite:///{root / 'auth.sqlite3'}"
            )
            store.ensure_schema()
            policy = _policy()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="authz-initial",
                    source="test",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    policy=policy,
                )
            )
            now = datetime.now(timezone.utc)
            manager = HumanSessionManager(config=_config(), session_store=store, now=lambda: now)
            session = manager.issue(
                replace(_identity(), role="admin"), GitHubOAuthCredential("never-in-http")
            )
            oauth = _StubFastApiGitHubOAuthClient(replace(_identity(), role="admin"))
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                human_session_manager=manager,
                github_oauth_client=oauth,
                record_store_factory=lambda: store,
            )
            now += timedelta(seconds=SESSION_RENEW_AFTER_SECONDS + 1)
            async with lifespan_client(app) as client:
                response = await client.get(
                    "/v1/auth/session", headers={"Cookie": manager.session_cookie_header(session)}
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("Set-Cookie", response.headers)
                self.assertNotIn("never-in-http", response.text + str(response.headers))
                self.assertEqual(response.json()["csrf_token"], manager.csrf_token(session))
                store.write_authz_policy_record(
                    LaunchplaneAuthzPolicyRecord(
                        record_id="authz-demoted",
                        source="test",
                        updated_at=now.isoformat(),
                        policy=_policy("read_only"),
                    )
                )
                demoted = await client.get("/v1/auth/session")
                self.assertEqual(demoted.json()["identity"]["role"], "read_only")
                store.write_authz_policy_record(
                    LaunchplaneAuthzPolicyRecord(
                        record_id="authz-removed",
                        source="test",
                        updated_at=now.isoformat(),
                        policy=LaunchplaneAuthzPolicy(),
                    )
                )
                revoked = await client.get("/v1/auth/session")
                self.assertEqual(revoked.status_code, 401)
                self.assertIsNone(store.read_session(session.session_id))
            store.close()
