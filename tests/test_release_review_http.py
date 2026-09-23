import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from httpx2 import Response

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.release_review import build_release_review
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _RejectingVerifier,
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests.test_product_review import _human
from tests.test_release_review import github_read, profile, seed


class ReleaseReviewHttpTests(unittest.IsolatedAsyncioTestCase):
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
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["operator"],
                            "roles": ["read_only"],
                            "products": ["example-site"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read", "product_profile.write"],
                        }
                    ]
                }
            ),
        )
        replacement = patch(
            "control_plane.http_app.current_release_review",
            side_effect=lambda **kwargs: build_release_review(
                store=self.store, profile=kwargs["profile"], read=github_read
            ),
        )
        replacement.start()
        self.addCleanup(replacement.stop)

    async def post(
        self,
        *,
        actor: str = "site-owner",
        github_id: int = 9001,
        outcome: str = "accepted",
        reason: str = "",
        digest: str = "",
        csrf: bool = True,
    ) -> Response:
        session = self.sessions.issue(_human(login=actor, github_id=github_id))
        headers = _browser_mutation_headers(self.sessions, session)
        if not csrf:
            headers.pop("X-CSRF-Token", None)
        current = build_release_review(store=self.store, profile=profile(), read=github_read)
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/release-review/decisions",
            headers=headers,
            payload={
                "product": "example-site",
                "checklist_digest": digest or current.checklist_digest,
                "decision": outcome,
                "reason": reason,
            },
        )

    async def test_owner_accepts_release_without_provider_or_merge_effects(self) -> None:
        session = self.sessions.issue(_human(login="site-owner", github_id=9001))
        before = await _asgi_get(
            self.app,
            "/v1/release-review?product=example-site",
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )
        self.assertEqual(before.status_code, 200, before.text)
        self.assertFalse(before.json()["review"]["approved"])
        after = await self.post()
        self.assertEqual(after.status_code, 200, after.text)
        self.assertTrue(after.json()["review"]["approved"])
        self.assertEqual(
            len(self.store.list_release_review_decision_records(product="example-site")), 1
        )
        self.assertEqual(self.store.list_deployment_records(), ())

    async def test_other_human_and_owner_override_are_denied(self) -> None:
        for args in (
            {"actor": "another-user", "github_id": 9002},
            {"outcome": "overridden", "reason": "Let it through"},
        ):
            with self.subTest(args=args):
                response = await self.post(**args)
                self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    async def test_operator_override_requires_reason_and_never_impersonates_owner(self) -> None:
        missing = await self.post(actor="operator", github_id=9003, outcome="overridden")
        self.assertEqual(missing.status_code, 400, missing.text)
        impersonation = await self.post(actor="operator", github_id=9003)
        self.assertEqual(impersonation.status_code, 403, impersonation.text)
        override = await self.post(
            actor="operator",
            github_id=9003,
            outcome="overridden",
            reason="Operator inspected the release.",
        )
        self.assertEqual(override.status_code, 200, override.text)
        record = self.store.list_release_review_decision_records(product="example-site")[0]
        self.assertEqual(record.actor_github_id, "9003")
        self.assertEqual(record.decision, "overridden")
        self.assertEqual(record.reason, "Operator inspected the release.")

    async def test_stale_checklist_and_missing_csrf_write_nothing(self) -> None:
        stale = await self.post(digest="f" * 64)
        self.assertEqual(stale.status_code, 409, stale.text)
        csrf = await self.post(csrf=False)
        self.assertEqual(csrf.status_code, 403, csrf.text)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    async def test_owner_can_reject_after_acceptance(self) -> None:
        await self.post()
        rejected = await self.post(
            outcome="changes_requested", reason="The repair prices are wrong."
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertFalse(rejected.json()["review"]["approved"])
        self.assertFalse(
            build_release_review(store=self.store, profile=profile(), read=github_read).approved
        )
