import unittest

import click
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import cast

from httpx2 import Response
from control_plane.contracts.artifact_identity import ArtifactAddonSource

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.release_review import build_release_review
from control_plane.release_review_record import _decision_effect, publish_release_decision
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
    async def test_progress_belongs_to_the_current_release_not_a_previous_invitation(self) -> None:
        from control_plane.client_release import ClientReleaseRunView
        from tests.test_release_review import HEAD

        self.store.write_product_profile_record(
            profile().model_copy(update={"release_on_acceptance": "promote"})
        )
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 200, accepted.text)
        decision = self.store.list_release_review_decision_records(product="example-site")[0]
        session = self.sessions.issue(_human(login="site-owner", github_id=9001))

        async def read() -> dict[str, object]:
            result = await _asgi_get(
                self.app,
                "/v1/release-review?product=example-site",
                headers={"Cookie": self.sessions.session_cookie_header(session)},
            )
            self.assertEqual(result.status_code, 200, result.text)
            return cast(dict[str, object], result.json())

        with patch(
            "control_plane.http_routes.release_review.read_client_release_run",
            return_value=ClientReleaseRunView(
                decision_record_id=decision.record_id,
                rollback_drill=False,
                state="passed",
                steps=(),
            ),
        ) as progress:
            self.assertIsNotNone((await read())["release_run"])
            # Finishing promotion changes the digest, but this is still the same
            # candidate on testing and production, not a new invitation.
            production = self.store.read_release_tuple_record(
                context_name="example-site", channel_name="prod"
            )
            self.store.write_release_tuple_record(
                production.model_copy(
                    update={"artifact_id": "artifact-testing", "repo_shas": {"example/site": HEAD}}
                )
            )
            self.assertIsNotNone((await read())["release_run"])
            # A fresh testing version must not inherit the completed run.
            testing = self.store.read_release_tuple_record(
                context_name="example-site", channel_name="testing"
            )
            artifact = self.store.read_artifact_manifest("artifact-testing")
            self.store.write_artifact_manifest(
                artifact.model_copy(
                    update={"artifact_id": "artifact-next", "source_commit": "d" * 40}
                )
            )
            self.store.write_release_tuple_record(
                testing.model_copy(
                    update={"artifact_id": "artifact-next", "repo_shas": {"example/site": "d" * 40}}
                )
            )
            progress.reset_mock()
            self.assertIsNone((await read())["release_run"])
            progress.assert_not_called()
            next_acceptance = await self.post()
            self.assertEqual(next_acceptance.status_code, 200, next_acceptance.text)
            self.assertIsNotNone(next_acceptance.json()["release_run"])
            current_decision = self.store.list_release_review_decision_records(
                product="example-site"
            )[0]
            self.assertNotEqual(current_decision.record_id, decision.record_id)
            self.assertEqual(
                progress.call_args.kwargs["decision"].record_id, current_decision.record_id
            )

    async def test_client_accepts_covered_shared_release_and_incomplete_or_stale_refuses(
        self,
    ) -> None:
        from tests.test_release_review import BASE, HEAD
        from tests.test_release_review_shared import SharedGitHub

        github = SharedGitHub()
        for instance, sha in (("prod", BASE), ("testing", HEAD)):
            artifact = self.store.read_artifact_manifest(f"artifact-{instance}")
            self.store.write_artifact_manifest(
                artifact.model_copy(
                    update={
                        "addon_sources": (
                            ArtifactAddonSource(repository="example/shared-addons", ref=sha),
                            ArtifactAddonSource(repository="example/disable-online", ref=sha),
                        )
                    }
                )
            )
        with patch("tests.test_release_review_http.github_read", side_effect=github.read):
            accepted = await self.post()
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertTrue(accepted.json()["review"]["approved"])
            digest = accepted.json()["review"]["checklist_digest"]
            github.notes = "Check the new shared sign-in behavior."
            stale = await self.post(digest=digest)
            self.assertEqual(stale.status_code, 409)
            for notes, uncovered in (
                ("", False),
                ("#52 has no Client test notes.", False),
                (github.notes, True),
            ):
                github.notes, github.uncovered = notes, uncovered
                refused = await self.post()
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertIn("release_checklist_incomplete", refused.text)

    async def test_accept_publishes_saved_decision_through_storage_lock(self) -> None:
        from tests.test_release_review_record import FakeReleaseIssues

        github = FakeReleaseIssues()
        with (
            patch(
                "control_plane.http_app.publish_release_decision", wraps=publish_release_decision
            ),
            patch(
                "control_plane.release_review_record.resolve_launchplane_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.release_review_record.resolve_delivery_github_app_id",
                return_value=77,
            ),
            patch(
                "control_plane.release_review_record.github_api_request", side_effect=github.request
            ),
            patch.object(
                self.store,
                "release_review_publication_lock",
                wraps=self.store.release_review_publication_lock,
            ) as publication_lock,
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.store.list_release_review_decision_records(product="example-site")[0]
        publication_lock.assert_called_once_with(record_id=saved.record_id)
        self.assertEqual(len(github.issues), 1)
        self.assertEqual(
            response.json()["review"]["latest_decision"]["release_issue_url"],
            saved.release_issue_url,
        )
        self.assertTrue(response.json()["review"]["approved"])

    async def test_publication_describes_the_saved_release_action(self) -> None:
        from tests.test_release_review_record import FakeReleaseIssues

        github = FakeReleaseIssues()
        cases = (
            ("held", "accepted", ""),
            ("promote", "accepted", "promote"),
            (
                "promote_with_rollback_drill",
                "accepted",
                "promote_with_rollback_drill",
            ),
            ("promote", "overridden", ""),
            ("promote", "changes_requested", ""),
        )
        effects: list[str] = []
        with (
            patch(
                "control_plane.http_app.publish_release_decision", wraps=publish_release_decision
            ),
            patch(
                "control_plane.release_review_record.resolve_launchplane_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.release_review_record.resolve_delivery_github_app_id",
                return_value=77,
            ),
            patch(
                "control_plane.release_review_record.github_api_request", side_effect=github.request
            ),
        ):
            for mode, outcome, start in cases:
                with self.subTest(mode=mode, outcome=outcome):
                    self.store.write_product_profile_record(
                        profile().model_copy(update={"release_on_acceptance": mode})
                    )
                    response = await self.post(
                        actor="operator" if outcome == "overridden" else "site-owner",
                        github_id=9003 if outcome == "overridden" else 9001,
                        outcome=outcome,
                        reason="Prices need review." if outcome != "accepted" else "",
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    saved = self.store.list_release_review_decision_records(product="example-site")[
                        0
                    ]
                    self.assertEqual(saved.release_start, start)
                    issue = github.issues[-1]
                    self.assertEqual(
                        saved.release_issue_url,
                        f"https://github.com/example/site/issues/{issue['number']}",
                    )
                    # Read the body submitted to publication, not a separate rendering.
                    body = str(issue["body"])
                    effect = _decision_effect(saved)
                    self.assertIn(effect, body)
                    # A universal description must not erase distinct recorded actions.
                    self.assertNotIn(effect, effects)
                    effects.append(effect)
                    self.assertIn(saved.record_id, body)
                    self.assertIn(saved.checklist_digest, body)
                    self.assertEqual(
                        response.json()["review"]["latest_decision"]["record_id"], saved.record_id
                    )
        self.assertEqual(self.store.list_deployment_records(), ())

    async def test_client_can_accept_again_after_a_stopped_release(self) -> None:
        from control_plane.client_release import ClientReleaseRunView

        self.store.write_product_profile_record(
            profile().model_copy(update={"release_on_acceptance": "promote"})
        )
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 200, accepted.text)
        previous = self.store.list_release_review_decision_records(product="example-site")[0]
        with patch(
            "control_plane.http_routes.release_review.read_client_release_run",
            return_value=ClientReleaseRunView(
                decision_record_id=previous.record_id,
                rollback_drill=False,
                state="stopped",
                steps=(),
            ),
        ):
            repeated = await self.post()
        self.assertEqual(repeated.status_code, 200, repeated.text)
        latest = self.store.list_release_review_decision_records(product="example-site")[0]
        self.assertNotEqual(previous.record_id, latest.record_id)
        self.assertEqual(latest.checklist_digest, previous.checklist_digest)

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
        publisher = patch(
            "control_plane.http_app.publish_release_decision",
            return_value="https://github.com/example/site/issues/99",
        )
        self.publisher = publisher.start()
        self.addCleanup(publisher.stop)

    async def post(
        self,
        *,
        actor: str = "site-owner",
        github_id: int = 9001,
        outcome: str = "accepted",
        reason: str = "",
        digest: str = "",
        csrf: bool = True,
        release_now: bool = False,
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
                "release_during_business_hours": release_now,
            },
        )

    async def test_business_hours_escape_requires_human_acceptance_and_audits_the_reason(
        self,
    ) -> None:
        self.store.write_product_profile_record(
            profile().model_copy(update={"release_on_acceptance": "promote"})
        )
        denied = await self.post(
            actor="other", github_id=999, reason="Urgent bug fix", release_now=True
        )
        self.assertEqual(denied.status_code, 403)
        missing_reason = await self.post(release_now=True)
        self.assertEqual(missing_reason.status_code, 400)
        normal = await self.post()
        normal_id = normal.json()["review"]["latest_decision"]["record_id"]
        urgent = await self.post(reason="Staff cannot save orders", release_now=True)
        self.assertEqual(urgent.status_code, 200, urgent.text)
        saved = urgent.json()["review"]["latest_decision"]
        self.assertNotEqual(saved["record_id"], normal_id)
        self.assertTrue(saved["release_during_business_hours"])
        record = self.store.read_release_review_decision_record(
            product="example-site", record_id=saved["record_id"]
        )
        self.assertEqual(record.reason, "Staff cannot save orders")
        self.assertEqual(record.actor_github_id, "9001")
        repeated = await self.post(reason="Staff cannot save orders", release_now=True)
        self.assertEqual(
            repeated.json()["review"]["latest_decision"]["record_id"], record.record_id
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

    async def test_identical_release_preserves_progress_and_refuses_new_decisions(self) -> None:
        from control_plane.client_release import ClientReleaseRunView

        await self.post()
        accepted = self.store.list_release_review_decision_records(product="example-site")[0]
        production = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="prod"
        )
        self.store.write_release_tuple_record(
            production.model_copy(update={"artifact_id": accepted.checklist.candidate.artifact_id})
        )
        run = ClientReleaseRunView(
            decision_record_id=accepted.record_id,
            rollback_drill=False,
            state="passed",
            steps=(),
        )
        with patch(
            "control_plane.http_routes.release_review.read_client_release_run", return_value=run
        ):
            session = self.sessions.issue(_human(login="site-owner", github_id=9001))
            current = await _asgi_get(
                self.app,
                "/v1/release-review?product=example-site",
                headers={"Cookie": self.sessions.session_cookie_header(session)},
            )
            self.assertEqual(current.status_code, 200, current.text)
            self.assertTrue(current.json()["review"]["no_new_release"])
            self.assertFalse(current.json()["review"]["required"])
            self.assertEqual(current.json()["release_run"], run.model_dump(mode="json"))
            for args in (
                {},
                {"outcome": "changes_requested", "reason": "Stale tab"},
                {
                    "actor": "operator",
                    "github_id": 9002,
                    "outcome": "overridden",
                    "reason": "Stale tab",
                },
            ):
                with self.subTest(args=args):
                    refused = await self.post(**args)
                    self.assertEqual(refused.status_code, 409, refused.text)
                    self.assertEqual(refused.json()["error"]["code"], "no_new_release")
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), (accepted,)
        )
        self.publisher.assert_called_once()

    async def test_client_acceptance_starts_the_release_unless_held_or_overridden(self) -> None:
        def newest() -> str:
            return self.store.list_release_review_decision_records(product="example-site")[
                0
            ].release_start

        held = await self.post()
        self.assertEqual(held.status_code, 200, held.text)
        self.assertEqual(held.json()["release_on_acceptance"], "held")
        self.assertEqual(newest(), "")
        self.store.write_product_profile_record(
            profile().model_copy(update={"release_on_acceptance": "promote_with_rollback_drill"})
        )
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["release_on_acceptance"], "promote_with_rollback_drill")
        self.assertEqual(newest(), "promote_with_rollback_drill")
        # A second click repeats the decision a running release depends on.
        started = self.store.list_release_review_decision_records(product="example-site")
        repeated = await self.post()
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), started
        )
        override = await self.post(
            actor="operator", github_id=9003, outcome="overridden", reason="Hand promotion."
        )
        self.assertEqual(override.status_code, 200, override.text)
        self.assertEqual(newest(), "")
        self.assertEqual(self.store.list_deployment_records(), ())

    async def test_retry_preserves_concurrent_publication_before_issue_lookup(self) -> None:
        self.publisher.side_effect = ValueError("Source-control write unavailable")
        pending = await self.post()
        self.assertEqual(pending.status_code, 200, pending.text)
        unpublished = self.store.list_release_review_decision_records(product="example-site")[0]
        published = unpublished.model_copy(
            update={"release_issue_url": "https://github.com/example/site/issues/98"}
        )

        def review_while_another_writer_publishes(**kwargs: object) -> object:
            snapshot = build_release_review(store=self.store, profile=profile(), read=github_read)
            self.store.write_release_review_decision_record(published)
            return snapshot

        def publish(**kwargs: object) -> str:
            self.assertEqual(
                self.store.list_release_review_decision_records(product="example-site"),
                (published,),
            )
            return "https://github.com/example/site/issues/100"

        self.publisher.side_effect = publish
        publication_count = self.publisher.call_count
        with patch(
            "control_plane.http_app.current_release_review",
            side_effect=review_while_another_writer_publishes,
        ):
            retry = await self.post()
        self.assertEqual(self.publisher.call_count, publication_count)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(
            retry.json()["review"]["latest_decision"]["release_issue_url"],
            published.release_issue_url,
        )
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), (published,)
        )

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

    async def open_urgent(
        self,
        *,
        actor: str = "operator",
        github_id: int = 9003,
        reason: str = "",
        digest: str = "",
        adopt_issue_number: int | None = None,
        adopt_comment_id: int | None = None,
        dry_run: bool = False,
    ) -> Response:
        session = self.sessions.issue(_human(login=actor, github_id=github_id))
        current = build_release_review(store=self.store, profile=profile(), read=github_read)
        return await _asgi_request(
            self.app,
            "POST",
            "/v1/release-review/urgent-openings",
            headers=_browser_mutation_headers(self.sessions, session),
            payload={
                "product": "example-site",
                "checklist_digest": digest or current.checklist_digest,
                "reason": reason,
                "adopt_issue_number": adopt_issue_number,
                "adopt_comment_id": adopt_comment_id,
                "dry_run": dry_run,
            },
        )

    async def test_receipt_adoption_reuses_admin_gate_and_never_opens_a_second_invitation(
        self,
    ) -> None:
        with (
            patch("control_plane.http_app.adopt_release_invitation", return_value=True) as adopt,
            patch("control_plane.http_app.publish_release_invitation") as publish,
        ):
            denied = await self.open_urgent(
                actor="site-owner",
                github_id=9001,
                reason="Adopt existing request.",
                adopt_issue_number=91,
                adopt_comment_id=600,
            )
            self.assertEqual(denied.status_code, 403)
            stale = await self.open_urgent(
                reason="Adopt existing request.",
                digest="f" * 64,
                adopt_issue_number=91,
                adopt_comment_id=600,
            )
            self.assertEqual(stale.status_code, 409)
            adopt.assert_not_called()
            for dry_run in (True, False):
                result = await self.open_urgent(
                    reason="Adopt existing request.",
                    adopt_issue_number=91,
                    adopt_comment_id=600,
                    dry_run=dry_run,
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(adopt.call_args.kwargs["dry_run"], dry_run)
            publish.assert_not_called()
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    async def test_only_an_admin_with_a_reason_opens_an_urgent_review(self) -> None:
        with patch(
            "control_plane.http_app.publish_release_invitation", return_value=True
        ) as invitation:
            for args, status in (
                ({"actor": "site-owner", "github_id": 9001, "reason": "Urgent."}, 403),
                ({"actor": "another-user", "github_id": 9002, "reason": "Urgent."}, 403),
                ({"reason": "   "}, 400),
                ({"reason": "Urgent.", "digest": "f" * 64}, 409),
            ):
                with self.subTest(args=args):
                    refused = await self.open_urgent(**args)
                    self.assertEqual(refused.status_code, status, refused.text)
            invitation.assert_not_called()
            opened = await self.open_urgent(reason="  Fixes a login security problem. ")
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertEqual(
                invitation.call_args.kwargs["urgent_reason"], "Fixes a login security problem."
            )
            self.assertEqual(
                invitation.call_args.kwargs["urgent_checklist_digest"],
                build_release_review(
                    store=self.store, profile=profile(), read=github_read
                ).checklist_digest,
            )
            invitation.return_value = False
            unopened = await self.open_urgent(reason="Fixes a login security problem.")
            self.assertEqual(unopened.status_code, 409, unopened.text)
            invitation.side_effect = click.ClickException("Response lost after delivery.")
            uncertain = await self.open_urgent(reason="Fixes a login security problem.")
            self.assertEqual(uncertain.status_code, 409, uncertain.text)
            self.assertIn("may already be open", uncertain.text)
        # Opening asks the Client; it records no decision and starts nothing.
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )
        self.assertEqual(self.store.list_deployment_records(), ())

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

    async def test_release_record_failure_preserves_decision_and_retry_reuses_it(self) -> None:
        self.publisher.side_effect = ValueError("Source-control write unavailable")
        pending = await self.post()
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertFalse(pending.json()["review"]["approved"])
        record = self.store.list_release_review_decision_records(product="example-site")[0]
        self.assertEqual(record.decision, "accepted")
        self.assertFalse(record.release_issue_url)
        self.assertFalse(
            build_release_review(store=self.store, profile=profile(), read=github_read).approved
        )
        self.publisher.side_effect = None
        retry = await self.post()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["review"]["approved"])
        saved = self.store.list_release_review_decision_records(product="example-site")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].record_id, record.record_id)
        self.assertEqual(saved[0].release_issue_url, "https://github.com/example/site/issues/99")

    async def test_shared_addon_changes_need_recorded_operator_review(self) -> None:
        artifact = self.store.read_artifact_manifest("artifact-testing")
        self.store.write_artifact_manifest(
            artifact.model_copy(
                update={
                    "addon_sources": (
                        ArtifactAddonSource(repository="example/shared", ref="d" * 40),
                    ),
                }
            )
        )
        refused = await self.post()
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )
        approved = await self.post(
            actor="operator",
            github_id=9003,
            outcome="overridden",
            reason="Reviewed the shared component change and tested its repair flow.",
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(
            build_release_review(store=self.store, profile=profile(), read=github_read).approved
        )
