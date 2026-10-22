"""Isolated rehearsal of the Director's temporary hotfix on the example test product.

A SQLite record store and synthetic providers stand in for production: the Director
starts a hotfix through the release-review route, the release worker queues its
backup and promotion, and the Client then accepts it or requests changes, which rolls
production back to their last accepted release.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
from unittest.mock import patch

from httpx2 import Response

from control_plane.client_release import (
    advance_client_releases,
    client_release_grant,
    client_release_grant_allows,
    live_temporary_hotfix,
)
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.release_review import build_release_review
from control_plane.release_invitation import publish_release_invitation
from control_plane.release_review_record import release_decision_issue_body
from control_plane.service_auth import BearerIdentityConfig, GitHubHumanIdentity
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _RejectingVerifier,
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests import test_client_release
from tests.test_client_release import _backup_binding, _deployment
from tests.test_odoo_prod_promotion_operation import _ADMINISTRATOR_POLICY, _policy_record
from tests.test_product_review import _human
from tests.test_release_review import decision, github_read, profile, seed

PRODUCT = "example-site"
DIRECTOR_ID = 123
CLIENT_ID = 9001


def _director() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="director",
        github_id=DIRECTOR_ID,
        name="Director",
        email="director@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


class TemporaryHotfixRehearsal(unittest.IsolatedAsyncioTestCase):
    # The worker fixture's helpers: Odoo admission, finishing operations, moving prod.
    def _admission(self, **kwargs: object) -> object:
        return test_client_release.ClientReleaseTests._admission(cast(Any, self), **kwargs)

    def finish(self, operation_id: str, status: Literal["pass", "fail"] = "pass") -> None:
        test_client_release.ClientReleaseTests.finish(cast(Any, self), operation_id, status)

    def set_prod(self, artifact_id: str) -> None:
        test_client_release.ClientReleaseTests.set_prod(cast(Any, self), artifact_id)

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.store.seed_authz_policy_if_absent(_policy_record(_ADMINISTRATOR_POLICY))
        self.store.write_deployment_record(_deployment("deployment-prod-1", "artifact-prod"))
        # The Client accepted the release production runs now: the rollback target.
        current = decision(self.store, date="2026-09-22T00:00:00Z")
        self.store.write_release_review_decision_record(
            current.model_copy(
                update={
                    "record_id": "release-accepted-earlier",
                    "checklist": current.checklist.model_copy(
                        update={
                            "candidate": current.checklist.production,
                            "database_compatibility": None,
                        }
                    ),
                }
            )
        )
        compiled = lambda **kwargs: build_release_review(  # noqa: E731
            store=self.store, profile=kwargs["profile"], read=github_read
        )
        for target, replacement in (
            ("control_plane.client_release.current_release_review", compiled),
            ("control_plane.http_app.current_release_review", compiled),
            ("control_plane.client_release.admit_odoo_prod_promotion_run", self._admission),
            ("control_plane.client_release.resolve_odoo_prod_rollback_target", lambda **_: None),
            (
                "control_plane.workflows.production_backup_gate.resolve_production_backup_binding",
                _backup_binding,
            ),
        ):
            replacement_patch = patch(target, side_effect=replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)
        self.publisher = patch(
            "control_plane.http_app.publish_release_decision",
            return_value="https://github.com/example/site/issues/99",
        ).start()
        self.addCleanup(patch.stopall)
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            authz_policy=_ADMINISTRATOR_POLICY.model_validate(
                {
                    **_ADMINISTRATOR_POLICY.model_dump(mode="json", exclude_none=True),
                    "github_humans": [
                        *_ADMINISTRATOR_POLICY.model_dump(mode="json")["github_humans"],
                        {
                            "logins": ["operator"],
                            "roles": ["read_only"],
                            "products": [PRODUCT],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read", "product_profile.write"],
                        },
                    ],
                }
            ),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-agent-read",
            ),
        )

    def advance(self) -> tuple[str, ...]:
        return advance_client_releases(store=self.store, control_plane_root=self.root)

    def digest(self) -> str:
        return build_release_review(
            store=self.store, profile=profile(), read=github_read
        ).checklist_digest

    async def post(
        self, identity: GitHubHumanIdentity, outcome: str, reason: str = "", digest: str = ""
    ) -> Response:
        session = self.sessions.issue(identity)
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/release-review/decisions",
            headers=_browser_mutation_headers(self.sessions, session),
            payload={
                "product": PRODUCT,
                "checklist_digest": digest or self.digest(),
                "decision": outcome,
                "reason": reason,
            },
        )

    async def read(self, identity: GitHubHumanIdentity) -> dict[str, Any]:
        session = self.sessions.issue(identity)
        response = await _asgi_get(
            self.app,
            f"/v1/release-review?product={PRODUCT}",
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return cast(dict[str, Any], response.json())

    def move_testing(self) -> None:
        """Newer work lands on testing after the hotfix."""

        newer = "d" * 40
        self.store.write_artifact_manifest(
            self.store.read_artifact_manifest("artifact-testing").model_copy(
                update={"artifact_id": "artifact-newer", "source_commit": newer}
            )
        )
        self.store.write_release_tuple_record(
            self.store.read_release_tuple_record(
                context_name="example-site", channel_name="testing"
            ).model_copy(
                update={"artifact_id": "artifact-newer", "repo_shas": {"example/site": newer}}
            )
        )

    def newest(self) -> ReleaseReviewDecisionRecord:
        return self.store.list_release_review_decision_records(product=PRODUCT, limit=1)[0]

    async def ship_hotfix(self) -> ReleaseReviewDecisionRecord:
        """The Director starts the hotfix and the worker puts it live."""

        self.assertTrue((await self.read(_director()))["can_start_temporary_hotfix"])
        started = await self.post(_director(), "temporary_hotfix", "Checkout is down.")
        self.assertEqual(started.status_code, 200, started.text)
        hotfix = self.newest()
        self.assertEqual((hotfix.decision, hotfix.release_start), ("temporary_hotfix", "promote"))
        # Releases are held for this product: the setting governs only the Client's Accept.
        self.assertEqual(
            self.store.read_product_profile_record(PRODUCT).release_on_acceptance, "held"
        )
        refused = await self.post(_human(login="site-owner", github_id=CLIENT_ID), "accepted")
        self.assertEqual(refused.status_code, 409, "the Client waits until the fix is live")

        (backup_id,) = self.advance()
        backup = self.store.read_verireel_prod_backup_gate_operation_record(backup_id)
        assert backup.authorization is not None
        self.assertEqual(backup.authorization.grant, "temporary_hotfix")
        self.assertEqual(backup.authorization.caller.github_id, DIRECTOR_ID)
        self.assertEqual(backup.authorization.release_decision_record_id, hotfix.record_id)
        self.finish(backup_id)
        (promotion_id,) = self.advance()
        promotion = self.store.read_odoo_prod_promotion_operation_record(promotion_id)
        self.assertEqual(
            promotion.request.expected_artifact_id, hotfix.checklist.candidate.artifact_id
        )
        self.assertEqual(promotion.authorization.grant, "temporary_hotfix")
        self.finish(promotion_id)
        self.set_prod("artifact-testing")
        self.store.write_deployment_record(_deployment("deployment-prod-2", "artifact-testing"))
        self.assertEqual(self.advance(), ())
        self.assertEqual(live_temporary_hotfix(self.store, profile()), hotfix)
        return hotfix

    async def test_client_accepts_the_live_hotfix_as_a_normal_release(self) -> None:
        hotfix = await self.ship_hotfix()
        client = _human(login="site-owner", github_id=CLIENT_ID)
        view = await self.read(client)
        self.assertTrue(view["reviewing_temporary_hotfix"])
        self.assertEqual(view["review"]["checklist_digest"], hotfix.checklist_digest)
        self.assertEqual(view["review"]["latest_decision"]["reason"], "Checkout is down.")

        accepted = await self.post(client, "accepted", digest=hotfix.checklist_digest)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual((self.newest().decision, self.newest().release_start), ("accepted", ""))
        self.assertEqual(self.advance(), (), "the accepted fix is already live")
        self.assertIsNone(live_temporary_hotfix(self.store, profile()))

    async def test_newer_testing_work_waits_until_the_client_decides_the_hotfix(self) -> None:
        hotfix = await self.ship_hotfix()
        self.move_testing()
        self.assertEqual(live_temporary_hotfix(self.store, profile()), hotfix)
        with (
            patch.dict(
                "os.environ", {"LAUNCHPLANE_PUBLIC_URL": "https://launchplane.example.invalid"}
            ),
            patch(
                "control_plane.release_invitation.current_release_review",
                side_effect=AssertionError("no invitation while the hotfix awaits the Client"),
            ),
        ):
            publish_release_invitation(
                store=self.store, control_plane_root=self.root, profile=profile()
            )

    async def test_client_rejection_rolls_back_to_their_last_accepted_release(self) -> None:
        hotfix = await self.ship_hotfix()
        client = _human(login="site-owner", github_id=CLIENT_ID)
        # Lost release records keep the hotfix under review, even when the Client
        # edits their reason; the retry republishes a rejection still carrying rollback.
        self.publisher.return_value = ""
        for reason in ("Search is broken.", "The fix broke search."):
            unpublished = await self.post(
                client, "changes_requested", reason, hotfix.checklist_digest
            )
            self.assertEqual(unpublished.status_code, 200, unpublished.text)
            self.assertEqual(self.advance(), (), "an unpublished rejection rolls nothing back")
            self.assertTrue((await self.read(client))["reviewing_temporary_hotfix"])
        pending = self.newest()
        self.publisher.return_value = "https://github.com/example/site/issues/100"
        rejected = await self.post(
            client, "changes_requested", "The fix broke search.", hotfix.checklist_digest
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        rejection = self.newest()
        self.assertEqual(rejection.record_id, pending.record_id)
        self.assertEqual(rejection.release_start, "rollback")
        # Until production leaves the rejected hotfix, no newer decision replaces it,
        # even on newer work testing now carries.
        self.move_testing()
        for identity, outcome in (
            (client, "accepted"),
            (_human(login="operator", github_id=4242), "overridden"),
            (_director(), "temporary_hotfix"),
        ):
            with self.subTest(outcome=outcome):
                refused = await self.post(identity, outcome, "Reason")
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertIn("temporary_hotfix_rollback_in_progress", refused.text)
        self.assertEqual(self.newest().record_id, rejection.record_id)

        (rollback_id,) = self.advance()
        rollback = self.store.read_odoo_prod_rollback_operation_record(rollback_id)
        self.assertEqual(rollback.target.artifact_id, "artifact-prod")
        self.assertEqual(rollback.target.deployment_record_id, "deployment-prod-1")
        self.assertEqual(rollback.authorization.grant, "client_release_acceptance")
        self.assertEqual(rollback.authorization.caller.github_id, CLIENT_ID)
        self.assertTrue(client_release_grant_allows(self.store, rollback.authorization))
        from control_plane.release_review import release_version

        def unreadable_testing(**kwargs: Any) -> Any:
            if kwargs["instance"] == "testing":
                raise FileNotFoundError("Testing evidence is unavailable")
            return release_version(**kwargs)

        with patch("control_plane.client_release.release_version", side_effect=unreadable_testing):
            # Odoo's existing queued rollback grant still requires readable tuples.
            self.assertFalse(client_release_grant_allows(self.store, rollback.authorization))
        self.finish(rollback_id)
        self.set_prod("artifact-prod")
        self.assertTrue(client_release_grant_allows(self.store, rollback.authorization))
        self.assertEqual(self.advance(), ())
        view = await self.read(client)
        self.assertFalse(view["reviewing_temporary_hotfix"])
        # Testing already carries a newer invitation. Once production has left
        # the hotfix, its completed rollback is historical, not this release.
        self.assertIsNone(view["release_run"])

    async def test_a_stopped_rollback_keeps_the_rejection_until_production_leaves_the_fix(
        self,
    ) -> None:
        hotfix = await self.ship_hotfix()
        client = _human(login="site-owner", github_id=CLIENT_ID)
        rejected = await self.post(client, "changes_requested", "Broken.", hotfix.checklist_digest)
        self.assertEqual(rejected.status_code, 200, rejected.text)
        (rollback_id,) = self.advance()
        self.finish(rollback_id, "fail")
        self.move_testing()
        refused = await self.post(client, "accepted")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("temporary_hotfix_rollback_in_progress", refused.text)
        # An admin rollback takes production off the rejected fix; decisions resume.
        self.set_prod("artifact-prod")
        resumed = await self.post(client, "accepted")
        self.assertEqual(resumed.status_code, 200, resumed.text)

    async def test_only_the_director_can_start_a_hotfix(self) -> None:
        client = _human(login="site-owner", github_id=CLIENT_ID)
        override_admin = _human(login="operator", github_id=4242)
        for identity in (client, override_admin):
            with self.subTest(login=identity.login):
                refused = await self.post(identity, "temporary_hotfix", "Checkout is down.")
                self.assertEqual(refused.status_code, 403, refused.text)
                self.assertIn("temporary_hotfix_denied", refused.text)
                self.assertFalse((await self.read(identity))["can_start_temporary_hotfix"])
        agent = await _asgi_request(
            self.app,
            "POST",
            "/v1/release-review/decisions",
            headers={"Authorization": "Bearer terminal-agent-token"},
            payload={
                "product": PRODUCT,
                "checklist_digest": self.digest(),
                "decision": "temporary_hotfix",
                "reason": "Checkout is down.",
            },
        )
        self.assertIn(agent.status_code, (401, 403), agent.text)
        no_reason = await self.post(_director(), "temporary_hotfix")
        self.assertEqual(no_reason.status_code, 400, no_reason.text)
        self.assertEqual(self.newest().record_id, "release-accepted-earlier")
        self.publisher.assert_not_called()

    async def test_hotfix_needs_a_rollback_target_and_a_gated_live_product(self) -> None:
        self.store.write_product_profile_record(
            profile().model_copy(update={"production_use": "prelaunch"})
        )
        prelaunch = await self.post(_director(), "temporary_hotfix", "Checkout is down.")
        self.assertEqual(prelaunch.status_code, 409, prelaunch.text)
        self.store.write_product_profile_record(profile())
        earlier = self.store.list_release_review_decision_records(product=PRODUCT)[0]
        self.store.write_release_review_decision_record(
            earlier.model_copy(
                update={
                    "record_id": "release-overridden",
                    "decision": "overridden",
                    "reason": "Admin hand promotion.",
                    "actor_github_id": "4242",
                    "decided_at": "2026-09-22T00:00:01Z",
                }
            )
        )
        with patch(
            "control_plane.http_routes.release_review.rejection_rollback_target",
            return_value=None,
        ):
            refused = await self.post(_director(), "temporary_hotfix", "Checkout is down.")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("temporary_hotfix_unavailable", refused.text)
        self.assertEqual(self.newest().record_id, "release-overridden")

    async def test_the_worker_stops_a_hotfix_once_the_director_is_no_longer_admin(self) -> None:
        started = await self.post(_director(), "temporary_hotfix", "Checkout is down.")
        self.assertEqual(started.status_code, 200, started.text)
        hotfix = self.newest()
        grant = client_release_grant(
            decision=hotfix,
            action="production_backup_gate.execute",
            context="example-site",
            authorized_at="2026-09-23T00:00:00Z",
        )
        self.assertTrue(client_release_grant_allows(self.store, grant))
        narrowed = _policy_record(
            _ADMINISTRATOR_POLICY.model_validate({"schema_version": 2, "github_humans": []}),
            revision=2,
        )
        with patch(
            "control_plane.client_release.read_active_authz_policy_record", return_value=narrowed
        ):
            self.assertFalse(client_release_grant_allows(self.store, grant))
            self.assertEqual(self.advance(), ())


class TemporaryHotfixContractTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'state.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.accepted = decision(self.store)

    def test_a_hotfix_is_the_directors_promotion_and_only_a_rejection_rolls_back(self) -> None:
        hotfix = self.accepted.model_dump()
        hotfix.update(
            decision="temporary_hotfix",
            reason="Checkout is down.",
            release_start="promote",
            actor_github_id=str(DIRECTOR_ID),
            actor_github_login="director",
        )
        ReleaseReviewDecisionRecord.model_validate(hotfix)
        for update in (
            {"actor_github_id": str(CLIENT_ID)},
            {"release_start": ""},
            {"release_start": "rollback"},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                ReleaseReviewDecisionRecord.model_validate({**hotfix, **update})
        for outcome in ("accepted", "overridden"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                ReleaseReviewDecisionRecord.model_validate(
                    {
                        **self.accepted.model_dump(),
                        "decision": outcome,
                        "reason": "Reason",
                        "release_start": "rollback",
                    }
                )

    def test_a_hotfix_grant_has_the_directors_admin_caller(self) -> None:
        for role in ("client", "read_only"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                DurableOperationAuthorization(
                    action="production_backup_gate.execute",
                    product=PRODUCT,
                    context="example-site",
                    instances=("prod",),
                    authorized_at="2026-09-23T00:00:00Z",
                    caller=DurableOperationCallerIdentity(
                        identity_type="github_human",
                        login="director",
                        github_id=DIRECTOR_ID,
                        role=role,
                    ),
                    grant="temporary_hotfix",
                    release_decision_record_id="release-hotfix",
                )

    def test_the_hotfix_record_tells_the_client_plainly_and_links_the_review(self) -> None:
        hotfix = ReleaseReviewDecisionRecord.model_validate(
            {
                **self.accepted.model_dump(),
                "decision": "temporary_hotfix",
                "reason": "Checkout is down for @everyone.",
                "release_start": "promote",
                "actor_github_id": str(DIRECTOR_ID),
                "actor_github_login": "director",
            }
        )
        body = release_decision_issue_body(
            hotfix,
            client_login="site-owner",
            review_url="https://launchplane.example.invalid/ui/owner-review?product=example-site",
        )
        self.assertIn("@site-owner", body)
        self.assertIn(
            "https://launchplane.example.invalid/ui/owner-review?product=example-site", body
        )
        # Reasons are data: their mentions are neutralized.
        self.assertNotIn("@everyone", body)


if __name__ == "__main__":
    unittest.main()
