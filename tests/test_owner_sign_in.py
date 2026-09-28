import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.product_profile_record import ProductOwnerProfile
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.release_review import build_release_review
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
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import (
    _human_session_from_payload,
    _human_session_payload,
)
from tests.http_app_test_support import (
    _RejectingVerifier,
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests.test_release_review import github_read, profile, seed
from tests.test_service import _FakeOAuth2Session

OWNER_ID = 9001


def _identity(*, github_id: int = OWNER_ID, role: str = "owner") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=f"user-{github_id}",
        github_id=github_id,
        name="",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role=role,  # type: ignore[arg-type]
    )


def _github_user(github_id: int) -> _FakeOAuth2Session:
    return _FakeOAuth2Session(
        {
            GITHUB_USER_URL: {"login": f"user-{github_id}", "id": github_id, "email": None},
            GITHUB_ORGS_URL: [],
            GITHUB_TEAMS_URL: [],
            GITHUB_EMAILS_URL: [],
        }
    )


class OwnerSignInIdentityTests(unittest.TestCase):
    def sign_in(self, *, github_id: int, policy: dict[str, object]) -> GitHubHumanIdentity:
        client = GitHubOAuthClient(_github_oauth_config())
        with patch.object(GitHubOAuthClient, "_new_session", return_value=_github_user(github_id)):
            return client.fetch_identity(
                code="github-code",
                code_verifier="verifier",
                authz_policy=LaunchplaneAuthzPolicy.model_validate(policy),
                is_product_owner=lambda candidate: candidate == OWNER_ID,
            )

    def test_named_owner_without_a_policy_role_signs_in_as_owner(self) -> None:
        identity = self.sign_in(github_id=OWNER_ID, policy={"github_humans": []})

        self.assertEqual(identity.role, "owner")

    def test_someone_who_owns_nothing_is_still_refused(self) -> None:
        with self.assertRaises(PermissionError):
            self.sign_in(github_id=OWNER_ID + 1, policy={"github_humans": []})

    def test_a_policy_role_takes_precedence_over_being_an_owner(self) -> None:
        identity = self.sign_in(
            github_id=OWNER_ID,
            policy={"github_humans": [{"github_ids": [OWNER_ID], "roles": ["read_only"]}]},
        )

        self.assertEqual(identity.role, "read_only")


class OwnerSessionBoundaryTests(unittest.TestCase):
    def test_cookie_readers_refuse_owner_sessions_unless_the_caller_opts_in(self) -> None:
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        cookie = sessions.session_cookie_header(sessions.issue(_identity()))

        self.assertIsNone(sessions.read_cookie(cookie))
        self.assertIsNone(sessions.read_cookie_without_renewal(cookie))
        self.assertIsNotNone(sessions.read_cookie(cookie, allow_owner=True))
        self.assertIsNotNone(sessions.read_cookie_without_renewal(cookie, allow_owner=True))

    def test_owner_identity_never_satisfies_a_policy_rule(self) -> None:
        unrestricted = GitHubHumanPolicyRule.model_validate(
            {"github_ids": [OWNER_ID], "actions": ["product_profile.read"]}
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [unrestricted.model_dump(mode="json")]}
        )
        owner = _identity()

        self.assertFalse(
            unrestricted.matches_principal(
                github_id=OWNER_ID,
                login=owner.login,
                organizations=frozenset(),
                teams=frozenset(),
                role="owner",
            )
        )
        self.assertFalse(
            policy.allows(
                identity=owner,
                action="product_profile.read",
                product="example-site",
                context="launchplane",
            )
        )
        self.assertTrue(
            policy.allows(
                identity=replace(owner, role="read_only"),
                action="product_profile.read",
                product="example-site",
                context="launchplane",
            )
        )

    def test_stored_owner_session_is_not_widened_to_read_only(self) -> None:
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        session = sessions.issue(_identity())

        restored = _human_session_from_payload(_human_session_payload(session))

        self.assertEqual(restored.identity.role, "owner")


class OwnerSessionHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = FilesystemRecordStore(Path(directory.name))
        seed(self.store)
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
        )
        for target, replacement in (
            (
                "control_plane.http_app.current_release_review",
                lambda **kwargs: build_release_review(
                    store=self.store, profile=kwargs["profile"], read=github_read
                ),
            ),
            (
                "control_plane.http_app.publish_release_decision",
                lambda **kwargs: "https://github.com/example/site/issues/99",
            ),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cookie(self, github_id: int = OWNER_ID) -> dict[str, str]:
        session = self.sessions.issue(_identity(github_id=github_id))
        return {"Cookie": self.sessions.session_cookie_header(session)}

    async def test_owner_reads_and_accepts_their_release(self) -> None:
        signed_in = await _asgi_get(self.app, "/v1/auth/session", headers=self.cookie())
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        self.assertEqual(signed_in.json()["identity"]["role"], "owner")

        review = await _asgi_get(
            self.app, "/v1/release-review?product=example-site", headers=self.cookie()
        )
        self.assertEqual(review.status_code, 200, review.text)

        session = self.sessions.issue(_identity())
        decision = await _asgi_request(
            self.app,
            "POST",
            "/v1/release-review/decisions",
            headers=_browser_mutation_headers(self.sessions, session),
            payload={
                "product": "example-site",
                "checklist_digest": review.json()["review"]["checklist_digest"],
                "decision": "accepted",
                "reason": "",
            },
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertTrue(decision.json()["review"]["approved"])

    async def test_owner_session_is_refused_everywhere_else(self) -> None:
        for path in ("/v1/products", "/v1/products/example-site"):
            with self.subTest(path=path):
                response = await _asgi_get(self.app, path, headers=self.cookie())
                self.assertEqual(response.status_code, 401, response.text)

    async def test_owner_session_ends_when_they_stop_being_the_owner(self) -> None:
        headers = self.cookie()
        self.store.write_product_profile_record(
            profile().model_copy(
                update={"owner": ProductOwnerProfile(github_id="7777", github_login="new-owner")}
            )
        )

        for path in ("/v1/auth/session", "/v1/release-review?product=example-site"):
            with self.subTest(path=path):
                response = await _asgi_get(self.app, path, headers=headers)
                self.assertEqual(response.status_code, 401, response.text)

    async def test_owner_role_for_someone_who_owns_nothing_is_refused(self) -> None:
        response = await _asgi_get(
            self.app, "/v1/release-review?product=example-site", headers=self.cookie(9002)
        )

        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
