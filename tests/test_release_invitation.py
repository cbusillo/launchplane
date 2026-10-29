from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from github_plan_status.client_test_notes import ENGINEERING_RELEASE_TESTS

from control_plane.contracts.release_review import (
    ReleaseReviewDecisionRecord,
    ReleaseReviewItem,
    ReleaseReviewStatus,
)
from control_plane.contracts.release_request import (
    ReleaseRequestDecisionRecord,
    ReleaseRequestSource,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.client_release import (
    ClientReleaseRunView,
    ClientReleaseStepView,
)
from control_plane.contracts.odoo_website_settings import (
    OdooWebsiteSettings,
    OdooWebsiteSettingsIntent,
)
from control_plane.odoo_website_settings import (
    OdooWebsiteSettingsRequest,
    plan_website_settings,
    prepare_website_settings,
)
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionSelector,
    MergeTrainHistoricalCompletionEntrySelector,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.merge_train_github import GitHubMergeTrainClient
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_historical_completion import HistoricalCompletionSnapshot
from control_plane.merge_train_historical_disposition import (
    HistoricalDispositionAuthority,
    HistoricalDispositionRequest,
    build_historical_disposition,
)
from control_plane.release_invitation import (
    WHY_NOW,
    ReleaseInvitationBackoff,
    main,
    publish_release_invitation,
    release_invitation_marker,
    release_request_issue_marker,
    adopt_release_invitation,
    advance_release_invitations,
    _request_marker,
    _last_client_decision,
)
from control_plane.release_review import (
    ReleaseReviewStore,
    build_release_review,
    release_version,
    checklist_digest,
)
from control_plane.release_request import record_release_request, release_request_id
from control_plane.release_invitation_changes import (
    ReleaseInvitationNotesUnavailable,
    ReleaseInvitationProvenanceUnavailable,
    client_invitation_changes,
)
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneMergeTrainBatchLandingPlanRow,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.merge_train import _merge_train_service_identity, _merge_train_service_policy
from tests.test_release_review import decision, github_read, profile, seed


class ReleaseInvitationTests(unittest.TestCase):
    review: ReleaseReviewStatus
    profile: LaunchplaneProductProfileRecord
    store: PostgresRecordStore

    def test_three_candidates_keep_request_decision_and_outcome_in_one_issue_each(self) -> None:
        from control_plane.release_invitation import _settle_release_record
        from control_plane.release_review_record import publish_release_decision

        with (
            patch("control_plane.release_review_record.github_api_request", self.github),
            patch(
                "control_plane.release_review_record.resolve_launchplane_github_token",
                return_value="delivery-token",
            ),
            patch(
                "control_plane.release_review_record.resolve_delivery_github_app_id",
                return_value=77,
                create=True,
            ),
        ):
            for index in range(3):
                self.assertTrue(self.publish())
                self.assertEqual(len(self.issues), index + 1)
                issue = self.issues[-1]
                self.assertIn(f"@{self.profile.owner.github_login}", issue["body"])
                assert self.review.checklist is not None
                saved = decision(self.store, date=self.now.isoformat()).model_copy(
                    update={
                        "checklist": self.review.checklist,
                        "checklist_digest": self.review.checklist_digest,
                        "release_issue_url": "",
                        "release_start": "promote",
                    }
                )
                self.store.create_release_review_decision_record_if_absent(saved)
                url = publish_release_decision(
                    store=cast(ReleaseReviewStore, self.store),
                    control_plane_root=self.root,
                    profile=self.profile,
                    decision=saved,
                )
                self.assertEqual(
                    url, f"https://github.com/{self.profile.repository}/issues/{issue['number']}"
                )
                saved = self.store.read_release_review_decision_record(
                    product=self.profile.product, record_id=saved.record_id
                )
                run = ClientReleaseRunView(
                    decision_record_id=saved.record_id,
                    rollback_drill=False,
                    state="passed",
                    steps=(),
                )
                with patch(
                    "control_plane.client_release.read_client_release_run", return_value=run
                ):
                    self.assertTrue(
                        _settle_release_record(
                            store=cast(ReleaseReviewStore, self.store),
                            control_plane_root=self.root,
                            profile=self.profile,
                            decision=saved,
                        )
                    )
                self.assertEqual(issue["state"], "closed")
                self.assertEqual(len(self.issues), index + 1)
                self.assertIn("Release review", issue["body"])
                self.now += timedelta(minutes=1)
                self.change_candidate(
                    artifact_id=f"next-{index}", source_commit=f"{index + 5:x}" * 40
                )
                checklist = self.review.checklist
                self.review = self.review.model_copy(
                    update={"checklist_digest": checklist_digest(checklist)}
                )

    def test_recorded_director_and_finish_line_requests_survive_restart_and_send_once(self) -> None:
        sources: tuple[ReleaseRequestSource, ...] = ("director_request", "finish_line")
        for source in sources:
            with self.subTest(source=source):
                self.posts.clear()
                self.issues.clear()
                self.issues.clear()
                self.profile = self.profile.model_copy(update={"release_request": None})
                self.store.write_product_profile_record(self.profile)
                reason = f"The recorded {source} calls for this release."
                self.profile = record_release_request(
                    store=cast(ReleaseReviewStore, self.store),
                    profile=self.profile,
                    review=self.review,
                    source=source,
                    reason=reason,
                    decision_reference="https://git.example.test/decisions/7",
                    actor="director",
                )
                saved = self.profile.release_request
                self.store.close()
                self.store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite3'}"
                )
                self.addCleanup(self.store.close)
                self.assertEqual(
                    self.store.read_product_profile_record(self.profile.product).release_request,
                    saved,
                )
                for _ in range(2):
                    advance_release_invitations(
                        store=cast(ReleaseReviewStore, self.store),
                        control_plane_root=self.root,
                        backoff=ReleaseInvitationBackoff(),
                    )
                self.assertEqual(len(self.issues), 1)
                self.assertIn(reason, self.issues[0]["body"])
                self.assertEqual(
                    self.store.list_release_review_decision_records(product=self.profile.product),
                    (),
                )

    def test_recorded_request_does_not_renew_for_new_notes_merges_or_client_decisions(self) -> None:
        self.profile = record_release_request(
            store=cast(ReleaseReviewStore, self.store),
            profile=self.profile,
            review=self.review,
            source="director_request",
            reason="Release the agreed work.",
            decision_reference="",
            actor="director",
        )
        advance_release_invitations(store=self.store, control_plane_root=self.root)
        posts = self.posts.copy()
        self.review = self.review.model_copy(update={"checklist_digest": "f" * 64})
        self.now += timedelta(days=4)
        advance_release_invitations(store=self.store, control_plane_root=self.root)
        self.change_candidate(source_commit="e" * 40)
        advance_release_invitations(store=self.store, control_plane_root=self.root)
        self.store.create_release_review_decision_record_if_absent(decision(self.store))
        advance_release_invitations(store=self.store, control_plane_root=self.root)
        self.assertEqual(self.posts, posts)

    def test_worker_continues_a_new_decision_before_an_older_receipt_is_due(self) -> None:
        def decide(reason: str) -> None:
            self.profile = record_release_request(
                store=cast(ReleaseReviewStore, self.store),
                profile=self.profile,
                review=self.review,
                source="director_request",
                reason=reason,
                decision_reference="",
                actor="director",
            )

        decide("Release the first reviewed work.")
        backoff = ReleaseInvitationBackoff()
        advance_release_invitations(store=self.store, control_plane_root=self.root, backoff=backoff)
        assert self.review.checklist is not None
        changed = self.review.checklist.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"owner_test_notes": "Check the updated repair prices."})
                    for item in self.review.checklist.items
                )
            }
        )
        self.review = self.review.model_copy(
            update={"checklist": changed, "checklist_digest": checklist_digest(changed)}
        )
        reason = "Release the newly reviewed work."
        decide(reason)
        advance_release_invitations(store=self.store, control_plane_root=self.root, backoff=backoff)
        self.assertEqual(len(self.issues), 2)
        self.assertIn(reason, self.issues[-1]["body"])

    def test_request_decision_cannot_change_a_profile_during_an_accepted_run(self) -> None:
        accepted = decision(self.store).model_copy(update={"release_start": "promote"})
        self.store.create_release_review_decision_record_if_absent(accepted)
        run = ClientReleaseRunView(
            decision_record_id=accepted.record_id, rollback_drill=False, state="waiting", steps=()
        )
        before = self.store.read_product_profile_record(self.profile.product)
        with patch("control_plane.client_release.read_client_release_run", return_value=run):
            with self.assertRaises(ValueError):
                record_release_request(
                    store=cast(ReleaseReviewStore, self.store),
                    profile=self.profile,
                    review=self.review,
                    source="director_request",
                    reason="Release the completed work.",
                    decision_reference="",
                    actor="director",
                )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), before)
        self.assertEqual(self.posts, [])

    def test_request_decision_requires_a_client_release_product_without_a_hotfix(self) -> None:
        for update in (
            {"production_use": "prelaunch"},
            {"release_on_acceptance": "director_standing"},
            {},
        ):
            with self.subTest(update=update):
                profile = self.profile.model_copy(update=update)
                self.store.write_product_profile_record(profile)
                with patch(
                    "control_plane.client_release.live_temporary_hotfix",
                    return_value=object() if not update else None,
                ):
                    with self.assertRaises(ValueError):
                        record_release_request(
                            store=cast(ReleaseReviewStore, self.store),
                            profile=profile,
                            review=self.review,
                            source="director_request",
                            reason="Release the completed work.",
                            decision_reference="",
                            actor="director",
                        )
                self.assertEqual(self.store.read_product_profile_record(profile.product), profile)
        self.assertEqual(self.posts, [])

    def test_request_record_preserves_a_concurrent_product_edit(self) -> None:
        compare_and_write = self.store.compare_and_write_product_profile_record
        edited = self.profile.model_copy(update={"display_name": "Updated site name"})

        def concurrent_edit(**kwargs: Any) -> Any:
            self.store.write_product_profile_record(edited)
            return compare_and_write(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", concurrent_edit):
            with self.assertRaises(ValueError):
                record_release_request(
                    store=cast(ReleaseReviewStore, self.store),
                    profile=self.profile,
                    review=self.review,
                    source="director_request",
                    reason="Release the completed work.",
                    decision_reference="",
                    actor="director",
                )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), edited)
        self.assertEqual(self.posts, [])

    def test_merges_without_release_request_never_notify_the_client(self) -> None:
        original: ReleaseReviewStatus = self.review
        for change in ("docs", "engineering", "client_accepted"):
            with self.subTest(change=change):
                self.posts.clear()
                self.issues.clear()
                self.issues.clear()
                self.review = original
                if change != "client_accepted":
                    self.make_engineering_only()
                    if change == "docs":
                        assert self.review.checklist is not None
                        self.review = self.review.model_copy(
                            update={
                                "checklist": self.review.checklist.model_copy(
                                    update={"items": (), "additional_changes": ()}
                                )
                            }
                        )
                advance_release_invitations(store=self.store, control_plane_root=self.root)
                self.assertEqual(self.posts, [])
                self.assertEqual(self.issues, [])
                self.assertEqual(self.issues, [])

    def test_manual_request_adoption_is_receipt_only_and_preserves_original(self) -> None:
        self.prepare_settings()
        self.add_release_test_change()
        assert self.review.checklist is not None
        candidate_marker = release_invitation_marker(
            self.profile.product,
            self.review.checklist.candidate.model_copy(update={"website_settings_sha256": ""}),
        )
        self.issues.append(
            {
                "number": 91,
                "state": "open",
                "body": release_request_issue_marker(self.profile.product),
                "performed_via_github_app": {"id": 88},
            }
        )
        original = {
            "id": 600,
            "created_at": self.now.isoformat(),
            "body": candidate_marker + "\nManual request already sent.",
            "performed_via_github_app": {"id": 88},
        }
        self.comments.append(original.copy())
        kwargs: dict[str, Any] = dict(
            store=cast(ReleaseReviewStore, self.store),
            control_plane_root=self.root,
            profile=self.profile,
            checklist_digest=self.review.checklist_digest,
            issue_number=91,
            comment_id=600,
            reason="Preserve already sent request.",
        )
        self.assertTrue(adopt_release_invitation(**kwargs, dry_run=True))
        self.assertEqual(self.posts, [])
        self.assertTrue(adopt_release_invitation(**kwargs, dry_run=False))
        self.assertTrue(adopt_release_invitation(**kwargs, dry_run=False))
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn("@site-owner", self.posts[0]["body"])
        self.assertTrue(self.publish())
        self.assertTrue(self.publish())
        self.assertEqual(len(self.issues), 2)
        self.assertEqual(self.issues[0]["state"], "closed")
        self.assertNotIn("@site-owner", self.issues[1]["body"])
        self.assertEqual(self.comments[0], original)
        self.assertIn(json.dumps(kwargs["reason"]), self.comments[1]["body"])

    def test_manual_adoption_refuses_other_candidate_and_changed_checklist(self) -> None:
        self.issues.append(
            {
                "number": 91,
                "state": "open",
                "body": release_request_issue_marker(self.profile.product),
            }
        )
        self.comments.append({"id": 600, "body": "unrelated", "created_at": self.now.isoformat()})
        for digest in (self.review.checklist_digest, "0" * 64):
            with self.subTest(digest=digest):
                self.assertFalse(
                    adopt_release_invitation(
                        store=cast(ReleaseReviewStore, self.store),
                        control_plane_root=self.root,
                        profile=self.profile,
                        checklist_digest=digest,
                        issue_number=91,
                        comment_id=600,
                        dry_run=False,
                    )
                )
        self.assertEqual(self.posts, [])

    def test_missing_lane_diagnostics_are_bounded_without_hiding_recovery(self) -> None:
        self.publish()
        backoff = ReleaseInvitationBackoff()
        with patch(
            "control_plane.release_invitation.release_version",
            side_effect=FileNotFoundError,
        ):
            with self.assertLogs("control_plane.release_invitation", level="WARNING") as logs:
                for _ in range(3):
                    advance_release_invitations(
                        store=self.store, control_plane_root=self.root, backoff=backoff
                    )
            self.assertEqual(len(logs.records), 1)
        with patch("control_plane.release_invitation.release_version", wraps=release_version):
            advance_release_invitations(
                store=self.store, control_plane_root=self.root, backoff=backoff
            )
        self.assertFalse(backoff.diagnostics)

    def test_adoption_cannot_create_a_second_trusted_destination(self) -> None:
        self.publish()
        assert self.review.checklist is not None
        self.issues.append(
            {
                "number": 92,
                "state": "open",
                "performed_via_github_app": {"id": 88},
                "body": release_request_issue_marker(self.profile.product),
            }
        )
        self.comments.append(
            {
                "id": 600,
                "created_at": self.now.isoformat(),
                "performed_via_github_app": {"id": 88},
                "issue_url": "https://api.github.com/repos/example/site/issues/92",
                "body": release_invitation_marker(
                    self.profile.product, self.review.checklist.candidate
                ),
            }
        )
        posts = len(self.posts)
        self.assertFalse(
            adopt_release_invitation(
                store=cast(ReleaseReviewStore, self.store),
                control_plane_root=self.root,
                profile=self.profile,
                checklist_digest=self.review.checklist_digest,
                issue_number=92,
                comment_id=600,
                dry_run=False,
            )
        )
        self.assertEqual(len(self.posts), posts)

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
        self.profile = profile().model_copy(update={"release_on_acceptance": "promote"})
        self.store.write_product_profile_record(self.profile)
        self.review = build_release_review(
            store=cast(ReleaseReviewStore, self.store), profile=self.profile, read=github_read
        )
        self.issues: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.lose_response = False
        self.read_count = 0
        self.now = datetime.now(UTC)
        self.public_origin = "https://launchplane.example.invalid"
        for target, replacement in (
            ("_now", lambda: self.now),
            ("monotonic", lambda: self.now.timestamp()),
            ("launchplane_public_origin_from_env", lambda: self.public_origin),
            ("current_release_review", self.read_review),
            ("resolve_launchplane_github_token", lambda **kwargs: "delivery-token"),
            ("resolve_delivery_github_app_id", lambda **kwargs: 77),
            ("github_api_request", self.github),
        ):
            patcher = patch(f"control_plane.release_invitation.{target}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The seeded change is Client-facing; engineering-only tests remove it.
        assert self.review.checklist is not None
        self.record_client_evidence(self.review.checklist.items[0])

    def add_client_change(self, number: int, *, request_only: bool = False) -> None:
        assert self.review.checklist is not None
        item = self.review.checklist.items[0].model_copy(
            update={
                "pull_request_number": number,
                "url": f"https://github.com/example/site/pull/{number}",
                "title": f"Engineering title {number}",
                "owner_test_notes": f"Check the new repair prices for change {number}.",
            }
        )
        self.review = self.review.model_copy(
            update={
                "checklist": self.review.checklist.model_copy(
                    update={"items": (*self.review.checklist.items, item)}
                )
            }
        )
        self.record_client_evidence(item, request_only=request_only)

    def record_client_evidence(
        self, item: ReleaseReviewItem, *, request_only: bool = False
    ) -> None:
        number = item.pull_request_number
        if request_only:
            self.store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id=f"feedback-{number}",
                    product=self.profile.product,
                    context=self.profile.preview.context or "example-site",
                    source="test",
                    requested_at=self.now.isoformat(),
                    repository=self.profile.repository,
                    anchor_repo="site",
                    anchor_pr_number=number,
                    anchor_pr_url=item.url,
                    status="ready",
                    marker="preview-feedback",
                    delivery_status="delivered",
                    comment_markdown=f"Record Accept or Request changes: https://launchplane.example.invalid/ui/owner-review?repository=example%2Fsite&pull_request={number}",
                )
            )
        else:
            self.store.write_product_review_decision_record(
                ProductReviewDecisionRecord(
                    record_id=f"preview-decision-{number}",
                    product=self.profile.product,
                    repository=self.profile.repository,
                    pull_request_number=number,
                    head_sha=item.head_sha,
                    decision="accepted",
                    owner_github_id=self.profile.owner.github_id,
                    owner_github_login=self.profile.owner.github_login,
                    decided_at=self.now.isoformat(),
                )
            )

    def test_review_request_repository_casing_does_not_hide_client_change(self) -> None:
        self.publish()
        self.add_client_change(43, request_only=True)
        feedback = self.store.list_preview_pr_feedback_records()[0]
        self.store.write_preview_pr_feedback_record(
            feedback.model_copy(
                update={
                    "repository": "Example/Site",
                    "comment_markdown": feedback.comment_markdown.replace(
                        "example%2Fsite", "EXAMPLE%2FSITE"
                    ),
                }
            )
        )
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.issues), 2)
        self.assertIn("@site-owner", self.issues[-1]["body"])
        self.assertIn("change 43", self.issues[-1]["body"])

    def test_preview_without_review_request_and_other_product_decision_stay_silent(self) -> None:
        self.publish()
        self.add_client_change(43, request_only=True)
        feedback = self.store.list_preview_pr_feedback_records()[0]
        self.store.write_preview_pr_feedback_record(
            feedback.model_copy(
                update={
                    "comment_markdown": "Preview ready at https://preview.example.invalid",
                }
            )
        )
        self.add_client_change(44)
        record = self.store.list_product_review_decision_records(
            repository=self.profile.repository, pull_request_number=44
        )[0]
        self.store.write_product_review_decision_record(
            record.model_copy(update={"product": "another-site"})
        )
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.issues), 2)
        self.assertIn("@site-owner", self.issues[-1]["body"])
        self.assertNotIn("change 43", self.issues[-1]["body"])
        self.assertNotIn("change 44", self.issues[-1]["body"])

    def test_requested_changes_are_client_facing_without_preview_acceptance(self) -> None:
        self.publish()
        self.add_client_change(43)
        record = self.store.list_product_review_decision_records(
            repository=self.profile.repository, pull_request_number=43
        )[0]
        self.store.write_product_review_decision_record(
            record.model_copy(
                update={"decision": "changes_requested", "reason": "Check the revised prices."}
            )
        )
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.issues), 2)
        self.assertIn("change 43", self.issues[-1]["body"])

    def seed_train_batch(self, *, two_features: bool = False) -> ReleaseReviewItem:
        self.add_client_change(43)
        if two_features:
            self.add_client_change(44)
        assert self.review.checklist is not None
        batch = self.review.checklist.items[0].model_copy(
            update={
                "pull_request_number": 99,
                "url": "https://github.com/example/site/pull/99",
                "title": "Launchplane batch: #41, #43, #44",
                "owner_test_notes": (
                    "### #43 Repair prices\n\nCheck the new repair prices for change 43."
                    + (
                        "\n\n### #44 Opening hours\n\nCheck the opening hours on the contact page."
                        if two_features
                        else ""
                    )
                ),
            }
        )
        plan = MergeTrainBatchLandingPlan(
            plan_id="plan-test",
            batch_id="batch-test",
            repository=self.profile.repository,
            base_branch="main",
            candidate_ref="launchplane/train/test",
            candidate_sha="d" * 40,
            policy_key="test-policy",
            policy_sha256="f" * 64,
            created_at=self.now.isoformat(),
            candidate_pull_request_number=99,
            entries=tuple(
                MergeTrainBatchLandingEntry(
                    pull_request_number=number,
                    position=position,
                    expected_head_sha=batch.head_sha,
                    expected_base_sha="a" * 40,
                    merge_method="merge",
                    status="merged",
                    merge_commit_sha=batch.merge_commit,
                )
                for position, number in enumerate(
                    (41, 43, 44) if two_features else (41, 43), start=1
                )
            ),
        )
        self.store.write_merge_train_batch_landing_plan_record(
            MergeTrainBatchLandingPlanRecord(
                record_id="landing-test",
                source="test",
                updated_at=self.now.isoformat(),
                landing_plan=plan,
            )
        )
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (batch,)})}
        )
        return batch

    def test_accepted_batch_sends_one_release_invitation_naming_each_feature(self) -> None:
        self.seed_train_batch(two_features=True)
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.issues), 1)
        body = self.issues[0]["body"]
        self.assertIn("Repair prices", body)
        self.assertIn("Opening hours", body)
        self.assertIn("pull/43", body)
        self.assertIn("pull/44", body)
        self.assertNotIn("Launchplane batch", body)
        self.assertNotIn("pull/99", body)
        self.assertIn("press **Accept** or **Request changes**", body)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )

    def seed_historical_batch(self) -> MergeTrainBatchLandingPlanRecord:
        batch = self.seed_train_batch(two_features=True)
        policy = build_test_merge_train_policy_record(repository=self.profile.repository)
        source = self.store.list_merge_train_batch_landing_plan_records()[0]
        payload = source.landing_plan.model_dump(mode="json")
        payload.pop("landing_plan_sha256")
        payload.update(
            candidate_sha=batch.head_sha,
            candidate_tree_sha="result-tree-3",
            candidate_sha256="b" * 64,
            policy_key=policy.policy.policies[0].policy_key,
            policy_sha256=policy.policy_sha256,
        )
        responses: list[object] = []
        base = {"commit": {"sha": "pinned-base", "commit": {"tree": {"sha": "base-tree"}}}}
        responses.append(base)
        for entry in payload["entries"]:
            position = entry["position"]
            parent = entry["expected_base_sha"] if position == 1 else f"observed-{position - 1}"
            parent_tree = "base-tree" if position == 1 else f"result-tree-{position - 1}"
            result_tree = f"result-tree-{position}"
            entry.update(
                status="planned",
                merge_commit_sha="",
                expected_head_tree_sha=f"head-tree-{position}",
                recorded_candidate_parent_sha=parent,
                recorded_candidate_parent_tree_sha=parent_tree,
                recorded_candidate_result_sha=f"constructed-{position}",
                recorded_candidate_result_tree_sha=result_tree,
            )
            responses.extend(
                (
                    {
                        "number": entry["pull_request_number"],
                        "state": "closed",
                        "merged": True,
                        "base": {"ref": "main"},
                        "head": {"sha": entry["expected_head_sha"]},
                        "merge_commit_sha": f"observed-{position}",
                    },
                    {
                        "sha": entry["expected_head_sha"],
                        "tree": {"sha": entry["expected_head_tree_sha"]},
                    },
                    {
                        "sha": f"observed-{position}",
                        "tree": {"sha": result_tree},
                        "parents": [{"sha": parent}, {"sha": entry["expected_head_sha"]}],
                    },
                    {"sha": parent, "tree": {"sha": parent_tree}},
                    {"status": "ahead", "merge_base_commit": {"sha": f"observed-{position}"}},
                )
            )
        responses.append(base)
        plan = MergeTrainBatchLandingPlan.model_validate(payload)
        source = source.model_copy(update={"landing_plan": plan})
        candidate = MergeTrainBatchCandidate(
            batch_id=plan.batch_id,
            repository=plan.repository,
            base_branch=plan.base_branch,
            base_sha=plan.entries[0].expected_base_sha,
            policy_key=plan.policy_key,
            policy_sha256=plan.policy_sha256,
            candidate_ref=plan.candidate_ref,
            candidate_sha=plan.candidate_sha,
            candidate_tree_sha=plan.candidate_tree_sha,
            candidate_sha256=plan.candidate_sha256,
            status="passed",
            created_at=plan.created_at,
            updated_at=plan.created_at,
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=entry.pull_request_number,
                    position=entry.position,
                    head_sha=entry.expected_head_sha,
                    head_tree_sha=entry.expected_head_tree_sha,
                )
                for entry in plan.entries
            ),
        )
        controller = MergeTrainControllerStateRecord(
            controller_key=build_merge_train_controller_key(
                repository=plan.repository, base_branch=plan.base_branch
            ),
            repository=plan.repository,
            base_branch=plan.base_branch,
            policy_key=plan.policy_key,
            policy_sha256=plan.policy_sha256,
            status="reconcile_required",
            updated_at=plan.created_at,
            active_action="land_batch",
            active_phase="merge_batch_entries",
            active_record_id=source.record_id,
            reconciliation_status="required",
        )
        identity = _merge_train_service_identity()
        request = HistoricalDispositionRequest(
            repository=plan.repository,
            base_branch=plan.base_branch,
            selector=MergeTrainHistoricalCompletionSelector(
                expected_active_record_id=source.record_id,
                expected_landing_plan_id=plan.plan_id,
                expected_effect_sha=plan.candidate_sha,
                expected_policy_sha256=plan.policy_sha256,
                expected_entries=tuple(
                    MergeTrainHistoricalCompletionEntrySelector(
                        position=entry.position,
                        pull_request_number=entry.pull_request_number,
                        expected_head_sha=entry.expected_head_sha,
                        expected_head_tree_sha=entry.expected_head_tree_sha,
                    )
                    for entry in plan.entries
                ),
            ),
            identity=identity,
            scope=idempotency_scope(identity),
            route_path="/v1/work-graph/merge-train/controller/run-once",
            idempotency_key="historical-invitation",
            request_fingerprint="historical-invitation",
        )
        observed = GitHubMergeTrainClient(
            transport=RecordingMergeTrainGitHubTransport(responses=tuple(responses))
        ).observe_historical_batch_completion(
            landing_plan=plan,
            observed_at=self.now.isoformat(),
        )
        record = build_historical_disposition(
            request=request,
            authority=HistoricalDispositionAuthority(
                policy=policy,
                authz=LaunchplaneAuthzPolicyRecord(
                    record_id="test-authz",
                    source="test",
                    updated_at=self.now.isoformat(),
                    policy=_merge_train_service_policy(),
                ),
            ),
            snapshot=HistoricalCompletionSnapshot(
                controller=controller,
                policy=policy,
                landing=source,
                candidate=MergeTrainBatchCandidateRecord(
                    record_id="candidate-test",
                    source="test",
                    updated_at=plan.created_at,
                    candidate=candidate,
                ),
            ),
            provider_evidence=observed,
            recorded_at=self.now.isoformat(),
            trace_id="historical-invitation",
        ).successor
        self.store.write_merge_train_batch_landing_plan_record(
            source.model_copy(update={"status": "superseded"})
        )
        self.persist_historical_batch(record)
        return record

    def persist_historical_batch(self, record: MergeTrainBatchLandingPlanRecord) -> None:
        # Rehearse the native writer's persisted payload. Generic writers refuse
        # historical records; this test never invokes the live disposition API.
        with self.store._session_factory() as session:
            session.merge(
                LaunchplaneMergeTrainBatchLandingPlanRow(
                    record_id=record.record_id,
                    status=record.status,
                    source=record.source,
                    updated_at=record.updated_at,
                    repository=record.landing_plan.repository,
                    base_branch=record.landing_plan.base_branch,
                    batch_id=record.landing_plan.batch_id,
                    plan_id=record.landing_plan.plan_id,
                    payload=record.model_dump(mode="json"),
                )
            )
            session.commit()

    def test_historical_batch_notifies_constituents_once_without_release_authority(self) -> None:
        record = self.seed_historical_batch()
        self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.issues), 1)
        self.assertIn("pull/43", self.issues[0]["body"])
        self.assertIn("pull/44", self.issues[0]["body"])
        self.assertNotIn("pull/99", self.issues[0]["body"])
        self.change_candidate(source_commit="e" * 40)
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.issues), 2)
        self.assertEqual(sum("@site-owner" in post.get("body", "") for post in self.posts), 2)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )
        saved = self.store.list_merge_train_batch_landing_plan_records(record_id=record.record_id)[
            0
        ]
        self.assertEqual(saved, record)
        self.assertTrue(
            all(
                entry.status == "stale" and not entry.merge_commit_sha
                for entry in saved.landing_plan.entries
            )
        )

    def test_incomplete_or_inconsistent_historical_provenance_refuses_delivery(self) -> None:
        record = self.seed_historical_batch()
        assert self.review.checklist is not None

        def review_batch() -> ReleaseReviewStatus:
            assert self.review.checklist is not None
            with patch(
                "control_plane.release_review.read_release_changes",
                return_value=(self.review.checklist.items, ()),
            ):
                return build_release_review(
                    store=cast(ReleaseReviewStore, self.store),
                    profile=self.profile,
                    read=github_read,
                )

        expected_review = review_batch()
        historical = record.historical_completion
        assert historical is not None
        observed = historical.provider_evidence
        wrong_tree = observed.entries[0].model_copy(
            update={"observed_merge_commit_tree_sha": "unrelated-tree"}
        )
        for change in (
            {"historical_completion": None},
            {"status": "superseded"},
            {
                "historical_completion": historical.model_copy(
                    update={
                        "provider_evidence": observed.model_copy(
                            update={"final_observed_base_sha": "moved-base"}
                        )
                    }
                )
            },
            {
                "historical_completion": historical.model_copy(
                    update={
                        "provider_evidence": observed.model_copy(
                            update={"entries": (wrong_tree, *observed.entries[1:])}
                        )
                    }
                )
            },
        ):
            with self.subTest(change=change):
                self.persist_historical_batch(record.model_copy(update=change))
                self.assertEqual(review_batch(), expected_review)
                with self.assertRaisesRegex(
                    ReleaseInvitationProvenanceUnavailable, "example/site#99"
                ):
                    self.publish(ReleaseInvitationBackoff())
                self.assertEqual(self.posts, [])
        self.persist_historical_batch(record)
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.issues), 1)

    def test_historical_batch_number_alone_cannot_bind_a_different_candidate(self) -> None:
        self.seed_historical_batch()
        assert self.review.checklist is not None
        batch = self.review.checklist.items[0]
        self.review = self.review.model_copy(
            update={
                "checklist": self.review.checklist.model_copy(
                    update={"items": (batch.model_copy(update={"head_sha": "9" * 40}),)}
                )
            }
        )
        with self.assertRaisesRegex(ReleaseInvitationProvenanceUnavailable, "example/site#99"):
            self.publish()
        self.assertEqual(self.posts, [])

    def test_conflicting_completed_batch_memberships_refuse_delivery(self) -> None:
        self.seed_historical_batch()
        source = self.store.list_merge_train_batch_landing_plan_records(record_id="landing-test")[0]
        assert self.review.checklist is not None
        payload = source.landing_plan.model_dump(mode="json")
        payload.pop("landing_plan_sha256")
        for entry in payload["entries"]:
            entry.update(
                status="merged", merge_commit_sha=self.review.checklist.items[0].merge_commit
            )
        payload["entries"][-1]["pull_request_number"] = 45
        self.store.write_merge_train_batch_landing_plan_record(
            source.model_copy(
                update={
                    "status": "active",
                    "landing_plan": MergeTrainBatchLandingPlan.model_validate(payload),
                }
            )
        )
        with self.assertRaises(ReleaseInvitationProvenanceUnavailable):
            self.publish()
        self.assertEqual(self.posts, [])

    def test_historical_engineering_only_batch_requests_release_acceptance(self) -> None:
        self.seed_historical_batch()
        for number in (43, 44):
            preview_decision = self.store.list_product_review_decision_records(
                repository=self.profile.repository, pull_request_number=number
            )[0]
            self.store.write_product_review_decision_record(
                preview_decision.model_copy(update={"product": "another-site"})
            )
        self.assertTrue(self.publish())
        self.assertTrue(
            any(line.startswith(WHY_NOW) for line in self.issues[0]["body"].splitlines())
        )

    def test_missing_batch_notes_are_visible_after_restart_and_recover_without_duplicate_ping(
        self,
    ) -> None:
        batch = self.seed_train_batch()
        broken = batch.model_copy(update={"owner_test_notes": "### #41 Engineering\nNo check."})

        def read_batch(item: ReleaseReviewItem) -> ReleaseReviewStatus:
            with patch(
                "control_plane.release_review.read_release_changes", return_value=((item,), ())
            ):
                return build_release_review(
                    store=cast(ReleaseReviewStore, self.store),
                    profile=self.profile,
                    read=github_read,
                )

        self.review = read_batch(broken)
        digest = self.review.checklist_digest
        self.assertTrue(self.review.checklist_complete)
        self.assertFalse(self.review.approved)
        self.assertIn(
            f"{self.profile.repository}#{batch.pull_request_number}",
            self.review.invitation_notes_error,
        )
        self.assertIn("#43", self.review.invitation_notes_error)
        self.assertNotIn(self.review.invitation_notes_error, self.review.blockers)
        backoff = ReleaseInvitationBackoff()
        with self.assertRaises(ReleaseInvitationNotesUnavailable):
            self.publish(backoff)
        self.assertEqual(self.posts, [])
        self.store.close()
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        restarted = read_batch(broken)
        self.assertEqual(restarted.invitation_notes_error, self.review.invitation_notes_error)
        self.assertEqual(restarted.checklist_digest, digest)

        self.review = read_batch(batch)
        self.assertFalse(self.review.invitation_notes_error)
        self.assertNotIn("invitation_notes_error", self.review.model_dump(mode="json"))
        self.publish(ReleaseInvitationBackoff())
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.issues), 1)
        self.assertIn("#43", self.issues[0]["body"])
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )

    def test_batch_note_example_cannot_replace_another_features_name_or_notes(self) -> None:
        batch = self.seed_train_batch(two_features=True)
        assert self.review.checklist is not None
        batch = batch.model_copy(
            update={
                "owner_test_notes": batch.owner_test_notes.replace(
                    "Check the new repair prices for change 43.",
                    "Check the new repair prices for change 43.\n\n```\n"
                    "### #44 Fake feature\nFake notes\n```",
                )
            }
        )
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (batch,)})}
        )
        self.publish()
        self.assertIn("Opening hours — Check the opening hours", self.issues[0]["body"])

    def test_duplicate_batch_member_sections_refuse_an_invitation(self) -> None:
        batch = self.seed_train_batch()
        assert self.review.checklist is not None
        batch = batch.model_copy(
            update={"owner_test_notes": batch.owner_test_notes + "\n\n### #43 Other\nOther notes"}
        )
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (batch,)})}
        )
        with self.assertRaisesRegex(ReleaseInvitationNotesUnavailable, "repeat"):
            self.publish()
        self.assertEqual(self.issues, [])

    def test_batch_names_and_legacy_note_headings_remain_literal(self) -> None:
        batch = self.seed_train_batch()
        assert self.review.checklist is not None
        for name in ("Support C#", "#"):
            with self.subTest(name=name):
                notes = (
                    f"### #43 {name}\n\n## Check this\nUse the feature.\n### Details\nCheck again."
                )
                checklist = self.review.checklist.model_copy(
                    update={"items": (batch.model_copy(update={"owner_test_notes": notes}),)}
                )
                changes = client_invitation_changes(
                    cast(ReleaseReviewStore, self.store), self.profile, checklist
                )
                change = next(iter(changes.values()))
                self.assertEqual(change.feature_name, name)
                self.assertIn("Use the feature.", change.item.owner_test_notes)
                self.assertIn("Check again.", change.item.owner_test_notes)

    def test_train_batch_uses_constituent_review_evidence_and_only_its_notes(self) -> None:
        self.publish()
        batch = self.seed_train_batch()
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.issues), 2)
        self.assertIn("change 43", self.issues[-1]["body"])
        self.assertIn("https://github.com/example/site/pull/43", self.issues[-1]["body"])
        self.assertNotIn("No manual check needed", self.issues[-1]["body"])
        self.assertNotIn("pull/99", self.issues[-1]["body"])
        broken = batch.model_copy(
            update={"owner_test_notes": "### #41 Engineering\n\nNo manual check needed."}
        )
        assert self.review.checklist is not None
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (broken,)})}
        )
        self.change_candidate(source_commit="f" * 40)
        with self.assertRaisesRegex(ReleaseInvitationNotesUnavailable, "#43"):
            self.publish()
        self.assertEqual(len(self.issues), 2)

    def make_engineering_only(self) -> None:
        assert self.review.checklist is not None
        item = self.review.checklist.items[0].model_copy(
            update={
                "pull_request_number": 50,
                "url": "https://github.com/example/site/pull/50",
                "title": "Engineering title 50",
                "owner_test_notes": "Nothing for the Client to test.",
            }
        )
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (item,)})}
        )

    def test_decided_engineering_request_does_not_record_acceptance(self) -> None:
        self.make_engineering_only()
        for backoff in (None, ReleaseInvitationBackoff()):
            self.assertTrue(self.publish(backoff))
        self.assertEqual(len(self.issues), 1)
        body = self.issues[0]["body"]
        self.assertIn("@site-owner", body)
        reason = next(
            line.removeprefix(WHY_NOW) for line in body.splitlines() if line.startswith(WHY_NOW)
        )
        self.assertTrue(reason.strip())
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    def test_accepted_settings_run_never_requests_again_during_rollback(self) -> None:
        self.make_engineering_only()
        self.prepare_settings()
        self.assertTrue(self.publish())
        assert self.review.checklist is not None
        accepted = decision(self.store, date=self.now.isoformat()).model_copy(
            update={
                "checklist": self.review.checklist,
                "release_start": "promote_with_rollback_drill",
            }
        )
        self.store.write_release_review_decision_record(accepted)
        original = self.issues[0].copy()
        posts = self.posts.copy()
        run = ClientReleaseRunView(
            decision_record_id=accepted.record_id,
            rollback_drill=True,
            state="running",
            steps=(
                ClientReleaseStepView(
                    step="backup-2", kind="backup", status="running", operation_id="backup"
                ),
            ),
        )
        unresolved = ClientReleaseStepView(
            step="rollback-1",
            kind="rollback",
            status="reconciliation_required",
            operation_id="rollback",
        )
        for pending in (
            run,
            run.model_copy(update={"state": "waiting"}),
            run.model_copy(update={"state": "stopped", "steps": (unresolved,)}),
        ):
            with (
                self.subTest(run=pending),
                patch("control_plane.client_release.read_client_release_run", return_value=pending),
            ):
                for backoff in (None, ReleaseInvitationBackoff()):
                    self.assertFalse(self.publish(backoff))
                self.assertFalse(self.publish(request_reason="Send the release request."))
        with patch("control_plane.client_release.read_client_release_run", return_value=run):
            self.change_candidate(source_commit="e" * 40)
            self.assertFalse(self.publish())
            with patch(
                "control_plane.client_release.read_client_release_run",
                return_value=run.model_copy(
                    update={
                        "state": "waiting",
                        "steps": (run.steps[0].model_copy(update={"status": "pass"}),),
                    }
                ),
            ):
                self.assertFalse(self.publish())
            self.change_candidate(source_commit=accepted.checklist.candidate.source_commit)
        self.assertEqual(self.posts, posts)
        self.assertEqual(self.issues, [original])
        self.assertEqual(self.edits, [])

    def test_destination_lookup_cannot_post_if_an_accepted_run_resumes(self) -> None:
        self.make_engineering_only()
        accepted = decision(self.store, date=self.now.isoformat()).model_copy(
            update={"release_start": "promote_with_rollback_drill"}
        )
        self.store.write_release_review_decision_record(accepted)
        self.review = self.review.model_copy(update={"checklist_digest": "f" * 64})
        run = ClientReleaseRunView(
            decision_record_id=accepted.record_id, rollback_drill=True, state="passed", steps=()
        )
        running = run.model_copy(
            update={
                "state": "running",
                "steps": (
                    ClientReleaseStepView(
                        step="backup-1", kind="backup", status="pending", operation_id="backup"
                    ),
                ),
            }
        )
        resumed = False

        def github(*, path: str, token: str, **kwargs: Any) -> object:
            nonlocal resumed
            result = self.github(path=path, token=token, **kwargs)
            if "?state=all" in path:
                resumed = True
            return result

        with (
            patch("control_plane.release_invitation.github_api_request", side_effect=github),
            patch(
                "control_plane.client_release.read_client_release_run",
                side_effect=lambda **kwargs: running if resumed else run,
            ),
        ):
            self.assertFalse(self.publish())
        self.assertTrue(resumed)
        self.assertEqual(self.issues, [])

    def test_superseded_unstarted_acceptance_does_not_block_the_next_request(self) -> None:
        from control_plane.client_release import read_client_release_run

        self.make_engineering_only()
        self.assertTrue(self.publish())
        accepted = decision(self.store, date=self.now.isoformat()).model_copy(
            update={"release_start": "promote"}
        )
        self.store.write_release_review_decision_record(accepted)
        self.now += timedelta(seconds=1)
        self.change_candidate(source_commit="e" * 40)
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "waiting")
        self.assertTrue(run.blocked_reason)
        self.assertFalse(any(step.status == "pass" for step in run.steps))
        self.assertTrue(self.publish())
        self.assertIn("@site-owner", self.issues[-1]["body"])
        self.change_candidate(source_commit=accepted.checklist.candidate.source_commit)
        production = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="prod"
        )
        artifact = self.store.read_artifact_manifest(production.artifact_id).model_copy(
            update={"artifact_id": "new-production", "source_commit": "d" * 40}
        )
        self.store.write_artifact_manifest(artifact)
        self.store.write_release_tuple_record(
            production.model_copy(update={"artifact_id": artifact.artifact_id})
        )
        self.refresh_settings_review()
        run = read_client_release_run(store=self.store, profile=self.profile, decision=accepted)
        assert run is not None
        self.assertEqual(run.state, "waiting")
        self.assertTrue(run.blocked_reason)
        self.assertTrue(self.publish())
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (accepted,),
        )

    def test_temporary_unstarted_release_waits_do_not_ask_again_after_notes_change(self) -> None:
        from control_plane.client_release import read_client_release_run
        from control_plane.contracts.odoo_business_hours import (
            BusinessHoursWindow,
            OdooBusinessHoursPolicy,
        )

        self.make_engineering_only()
        self.profile = self.profile.model_copy(
            update={"odoo_business_hours": OdooBusinessHoursPolicy(company_id=7)}
        )
        self.store.write_product_profile_record(self.profile)
        self.assertTrue(self.publish())
        accepted = decision(self.store, date=self.now.isoformat()).model_copy(
            update={"release_start": "promote"}
        )
        self.review = self.review.model_copy(update={"checklist_digest": "f" * 64})
        original = self.issues[0].copy()
        observed_reasons: set[str] = set()
        for window, draining, release_url in (
            (
                BusinessHoursWindow(applies=True, blocked_reason="business open"),
                False,
                accepted.release_issue_url,
            ),
            (BusinessHoursWindow(), True, accepted.release_issue_url),
            (BusinessHoursWindow(), False, ""),
        ):
            saved = accepted.model_copy(update={"release_issue_url": release_url})
            self.store.write_release_review_decision_record(saved)
            with (
                self.subTest(window=window, draining=draining, published=bool(release_url)),
                patch(
                    "control_plane.odoo_business_hours.read_business_hours_window",
                    return_value=window,
                ),
                patch(
                    "control_plane.service_deploy_drain.read_status",
                    return_value={"admission_paused": draining},
                ),
            ):
                run = read_client_release_run(
                    store=self.store, profile=self.profile, decision=saved
                )
                assert run is not None
                self.assertEqual(run.state, "waiting")
                self.assertTrue(run.blocked_reason)
                if window.blocked_reason:
                    self.assertEqual(run.blocked_reason, window.blocked_reason)
                self.assertNotIn(run.blocked_reason, observed_reasons)
                observed_reasons.add(run.blocked_reason)
                self.assertFalse(self.publish(ReleaseInvitationBackoff()))
                self.assertFalse(self.publish(request_reason="Send the release request."))
        self.assertEqual(self.issues, [original])
        self.assertEqual(self.edits, [])

    def add_release_test_change(self) -> ReleaseReviewItem:
        assert self.review.checklist is not None
        item = self.review.checklist.items[0].model_copy(
            update={
                "pull_request_number": 51,
                "url": "https://github.com/example/site/pull/51",
                "owner_test_notes": ENGINEERING_RELEASE_TESTS
                + "\nCheck login and logout on the testing site.",
            }
        )
        self.review = self.review.model_copy(
            update={
                "checklist": self.review.checklist.model_copy(
                    update={"items": (*self.review.checklist.items, item)}
                )
            }
        )
        return item

    def prepare_settings(self, *, key: str = "G-TEST123") -> None:
        self.profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"base_url": "https://site.example"})
                    if lane.instance == "prod"
                    else lane
                    for lane in self.profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        candidate = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="testing"
        )
        request = OdooWebsiteSettingsRequest(
            product=self.profile.product,
            context="example-site",
            website_settings=OdooWebsiteSettingsIntent(
                artifact_id=candidate.artifact_id,
                settings=OdooWebsiteSettings(
                    website_id=1,
                    public_origin="https://site.example",
                    public_indexing_enabled=True,
                    google_analytics_key=key,
                ),
            ),
            reason="Prepare release settings",
        )
        plan = plan_website_settings(self.store, request)
        prepared = prepare_website_settings(
            self.store,
            request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan["plan_sha256"]}
            ),
        )
        self.assertTrue(prepared["read_back_matches"])
        self.refresh_settings_review()

    def refresh_settings_review(self) -> None:
        assert self.review.checklist is not None
        with patch(
            "control_plane.release_review.read_release_changes",
            return_value=(self.review.checklist.items, ()),
        ):
            self.review = build_release_review(
                store=cast(ReleaseReviewStore, self.store), profile=self.profile, read=github_read
            )

    def test_prepared_settings_open_one_request_without_preview_evidence(self) -> None:
        self.make_engineering_only()
        self.prepare_settings()
        assert self.review.checklist is not None
        production = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="prod"
        )
        candidate = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="testing"
        )
        self.store.write_release_tuple_record(
            production.model_copy(
                update={"artifact_id": candidate.artifact_id, "repo_shas": candidate.repo_shas}
            )
        )
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": ()})}
        )
        self.refresh_settings_review()
        self.assertTrue(self.publish())
        self.assertTrue(self.publish(ReleaseInvitationBackoff()))
        self.assertEqual(len(self.issues), 1)
        self.assertIn("@site-owner", self.issues[0]["body"])
        assert self.review.checklist is not None
        self.assertIn(
            release_invitation_marker(self.profile.product, self.review.checklist.candidate),
            self.issues[0]["body"],
        )
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )

    def test_prepared_settings_include_engineering_release_steps(self) -> None:
        self.make_engineering_only()
        item = self.add_release_test_change()
        self.prepare_settings()
        self.assertTrue(self.publish())
        self.assertIn(item.owner_test_notes.splitlines()[1], self.issues[0]["body"])
        self.assertEqual(len(self.issues), 1)

    def test_settings_request_recovers_lost_response_without_a_duplicate(self) -> None:
        self.make_engineering_only()
        self.prepare_settings()
        self.assertTrue(self.publish())
        self.prepare_settings(key="G-NEXT456")
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertTrue(self.publish(ReleaseInvitationBackoff()))
        self.assertEqual(len(self.issues), 2)
        self.assertEqual(sum("@site-owner" in comment["body"] for comment in self.issues), 2)

    def test_unqualified_settings_never_open_a_request(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyResult

        self.make_engineering_only()
        production = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="prod"
        )
        candidate = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="testing"
        )
        self.store.write_release_tuple_record(
            production.model_copy(update={"artifact_id": candidate.artifact_id})
        )
        self.prepare_settings()
        stored = self.store.read_odoo_instance_override_record(
            context_name="example-site", instance_name="prod"
        )
        assert stored.website_settings is not None
        for replacement in (
            stored.model_copy(update={"website_settings": None}),
            stored.model_copy(
                update={
                    "website_settings": stored.website_settings.model_copy(
                        update={"artifact_id": "stale"}
                    )
                }
            ),
            stored.model_copy(
                update={
                    "last_apply": OdooOverrideApplyResult(
                        website_settings=stored.website_settings, website_settings_verified=False
                    )
                }
            ),
            stored.model_copy(
                update={
                    "last_apply": OdooOverrideApplyResult(
                        website_settings=stored.website_settings.model_copy(
                            update={"artifact_id": candidate.artifact_id}
                        )
                    )
                }
            ),
        ):
            with self.subTest(replacement=replacement):
                self.store.write_odoo_instance_override_record(replacement)
                self.refresh_settings_review()
                self.assertFalse(self.publish(ReleaseInvitationBackoff()))
                self.assertEqual((self.issues, self.posts), ([], []))

    def test_release_test_steps_appear_on_decided_engineering_request(self) -> None:
        self.make_engineering_only()
        item = self.add_release_test_change()
        for backoff in (None, ReleaseInvitationBackoff()):
            self.assertTrue(self.publish(backoff))
        body = self.issues[0]["body"]
        self.assertIn(item.url, body)
        self.assertIn(item.owner_test_notes.splitlines()[1], body)
        self.assertNotIn(ENGINEERING_RELEASE_TESTS, body)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )
        assert self.review.checklist is not None
        accepted = decision(self.store, date=self.now.isoformat()).model_copy(
            update={"checklist": self.review.checklist}
        )
        self.store.write_release_review_decision_record(accepted)
        posts = len(self.posts)
        self.now += timedelta(seconds=1)
        self.change_candidate(source_commit="e" * 40)
        self.assertTrue(self.publish(ReleaseInvitationBackoff()))
        self.assertEqual(len(self.posts), posts + 1)
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product),
            (accepted,),
        )

    def test_batch_release_test_steps_use_the_constituent_without_preview_evidence(self) -> None:
        batch = self.seed_train_batch()
        assert self.review.checklist is not None
        steps = "Check login and logout on the testing site."
        notes = f"### #41 Login reliability\n{ENGINEERING_RELEASE_TESTS}\n{steps}\n\n"
        self.review = self.review.model_copy(
            update={
                "checklist": self.review.checklist.model_copy(
                    update={
                        "items": (
                            batch.model_copy(
                                update={"owner_test_notes": notes + batch.owner_test_notes}
                            ),
                        )
                    }
                )
            }
        )
        with patch.object(self.store, "list_product_review_decision_records", return_value=()):
            self.assertTrue(self.publish())
        self.assertIn(steps, self.issues[0]["body"])
        self.assertTrue(self.publish())
        self.assertTrue(self.publish())
        body = self.issues[-1]["body"]
        self.assertIn("https://github.com/example/site/pull/41", body)
        self.assertIn("https://github.com/example/site/pull/43", body)
        self.assertIn(steps, body)
        self.assertNotIn(batch.url, body)
        self.assertNotIn(ENGINEERING_RELEASE_TESTS, body)

    def test_decided_request_states_reason_and_still_needs_client_acceptance(self) -> None:
        self.make_engineering_only()
        # A reason written for an earlier checklist never opens the current one.
        self.assertFalse(self.publish(request_reason="Security fix.", request_digest="f" * 64))
        self.assertEqual((self.issues, self.posts), ([], []))
        self.assertTrue(
            self.publish(request_reason="Fixes a login security problem.\nPing @someone")
        )
        self.assertEqual(len(self.issues), 1)
        body = self.issues[-1]["body"]
        self.assertIn("Hi @site-owner,", body)
        self.assertIn(
            "**Why now:** Fixes a login security problem. Ping @\u200bsomeone", body.splitlines()
        )
        self.assertIn("there is nothing new to test", body)
        self.assertIn("Accepting starts the release", body)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )
        # A deliberate opening of an existing request confirms it without another
        # mention or replacing the original reason.
        posts = len(self.posts)
        self.assertTrue(self.publish(request_reason="A later reason."))
        self.assertEqual(len(self.posts), posts)
        self.assertIn("**Why now:** Fixes a login security problem.", self.issues[-1]["body"])
        self.assertNotIn("A later reason.", self.issues[-1]["body"])
        self.change_candidate(source_commit="e" * 40)
        self.assertTrue(self.publish(request_reason="Release the follow-up repair."))
        self.assertEqual(len(self.issues), 2)
        assert self.profile.release_request is not None
        self.assertIn(self.profile.release_request.reason, self.issues[-1]["body"])

    def test_new_decision_after_engineering_notes_change_requests_acceptance(self) -> None:
        self.make_engineering_only()
        self.assertTrue(self.publish(request_reason="Security fix."))
        accepted = decision(self.store, date=self.now.isoformat())
        self.store.create_release_review_decision_record_if_absent(accepted)
        self.review = self.review.model_copy(update={"latest_decision": accepted, "approved": True})
        self.assertIs(self.publish(), False)

        self.now += timedelta(seconds=1)
        assert self.review.checklist is not None
        changed = self.review.checklist.model_copy(
            update={
                "items": tuple(
                    item.model_copy(
                        update={"owner_test_notes": "Check the additional login repair."}
                    )
                    for item in self.review.checklist.items
                )
            }
        )
        self.review = self.review.model_copy(
            update={
                "checklist": changed,
                "checklist_digest": checklist_digest(changed),
                "latest_decision": None,
                "approved": False,
            }
        )
        posts = len(self.posts)
        self.assertIs(self.publish(ReleaseInvitationBackoff()), True)
        self.assertEqual(len(self.posts), posts + 1)
        self.assertTrue(self.publish(request_reason="Updated security fix."))
        self.assertEqual(len(self.posts), posts + 1)
        assert self.profile.release_request is not None
        self.assertIn(self.profile.release_request.reason, self.issues[-1]["body"])
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), (accepted,)
        )

    def test_direct_retry_does_not_send_the_workers_due_reminder(self) -> None:
        self.make_engineering_only()
        self.assertTrue(self.publish(request_reason="Security fix."))
        posts = len(self.posts)
        self.now += timedelta(days=3)
        self.assertTrue(self.publish(request_reason="Security fix."))
        self.assertEqual(len(self.posts), posts)
        self.assertTrue(self.publish())
        self.assertEqual(len(self.posts), posts + 1)

    def read_review(self, **_kwargs: Any) -> ReleaseReviewStatus:
        self.read_count += 1
        return self.review

    def github(self, *, path: str, token: str, **kwargs: Any) -> object:
        self.assertEqual(token, "delivery-token")
        if path == "/installation/token" and kwargs.get("method") == "DELETE":
            return None
        self.assertTrue(path.startswith("/repos/example/site/issues"))
        if kwargs.get("method") == "PATCH":
            if re.fullmatch(r"/repos/example/site/issues/[0-9]+", path):
                issue = next(issue for issue in self.issues if path.endswith(f"/{issue['number']}"))
                issue.update(kwargs["body"])
                self.edits.append(kwargs["body"])
                if self.lose_response:
                    self.lose_response = False
                    raise TimeoutError("response lost after issue update")
                return issue.copy()
            comment_id = int(path.rsplit("/", 1)[1])
            comment = next(comment for comment in self.comments if comment["id"] == comment_id)
            comment.update(kwargs["body"], updated_at=self.now.isoformat())
            self.edits.append(kwargs["body"])
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("response lost after edit")
            return comment.copy()
        if kwargs.get("method") == "POST":
            body = kwargs["body"]
            self.posts.append(body)
            if path.endswith("/comments"):
                result = {
                    "id": max((item["id"] for item in self.comments), default=0) + 1,
                    "issue_number": int(path.split("/")[-2]),
                    "created_at": self.now.isoformat(),
                    "performed_via_github_app": {"id": 77},
                    **body,
                }
                self.comments.append(result)
                if self.lose_response:
                    self.lose_response = False
                    raise TimeoutError("response lost after publication")
                return result
            issue = {
                "number": 91 + len(self.issues),
                "state": "open",
                "created_at": self.now.isoformat(),
                "performed_via_github_app": {"id": 77},
                **body,
            }
            self.issues.append(issue)
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError("response lost after issue publication")
            return issue
        if "/comments/" in path:
            result = next(
                comment for comment in self.comments if path.endswith(f"/{comment['id']}")
            )
            return {"issue_url": "https://api.github.com/repos/example/site/issues/91", **result}
        if re.fullmatch(r"/repos/example/site/issues/[0-9]+", path):
            return next(
                issue.copy() for issue in self.issues if path.endswith(f"/{issue['number']}")
            )
        # Existing fixtures model records emitted by this fake Delivery App.
        return [
            {"performed_via_github_app": {"id": 77}, **item}
            for item in (
                [
                    comment
                    for comment in self.comments
                    if comment.get("issue_number", 91) == int(path.split("/")[-2])
                ]
                if "/comments?" in path
                else self.issues
            )
        ]

    def publish(
        self,
        backoff: ReleaseInvitationBackoff | None = None,
        *,
        request_reason: str = "",
        request_digest: str = "",
    ) -> bool:
        if self.review.checklist is None:
            return publish_release_invitation(
                store=cast(ReleaseReviewStore, self.store),
                control_plane_root=self.root,
                profile=self.profile,
                backoff=backoff,
            )
        self.review = self.review.model_copy(
            update={"checklist_digest": checklist_digest(self.review.checklist)}
        )
        production = release_version(store=self.store, profile=self.profile, instance="prod")
        candidate = release_version(store=self.store, profile=self.profile, instance="testing")
        if production == candidate:
            return publish_release_invitation(
                store=cast(ReleaseReviewStore, self.store),
                control_plane_root=self.root,
                profile=self.profile,
                backoff=backoff,
            )
        last_decision = _last_client_decision(cast(ReleaseReviewStore, self.store), self.profile)
        client_decision_id = last_decision.record_id if last_decision else ""
        request_id = release_request_id(
            self.profile, request_digest or self.review.checklist_digest, client_decision_id
        )
        request = self.profile.release_request
        if (
            request is None
            or request.record_id != request_id
            or request.production != production
            or request.candidate != candidate
            or request_digest
        ):
            assert self.review.checklist is not None
            request = ReleaseRequestDecisionRecord(
                record_id=request_id,
                product=self.profile.product,
                repository=self.profile.repository,
                owner_github_id=self.review.checklist.owner_github_id,
                checklist_digest=request_digest or self.review.checklist_digest,
                production=production,
                candidate=candidate,
                client_decision_record_id=client_decision_id,
                source="director_request",
                reason=request_reason or "Release the completed work.",
                actor="director",
                decided_at=self.now.isoformat(),
            )
            self.profile = self.profile.model_copy(update={"release_request": request})
            self.store.write_product_profile_record(self.profile)
        return publish_release_invitation(
            store=cast(ReleaseReviewStore, self.store),
            control_plane_root=self.root,
            profile=self.profile,
            backoff=backoff,
            send_reminder=not request_reason,
        )

    def change_candidate(self, **update: str) -> None:
        assert self.review.checklist is not None
        checklist = self.review.checklist
        release = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="testing"
        )
        artifact = self.store.read_artifact_manifest(release.artifact_id).model_copy(
            update={key: value for key, value in update.items() if key != "shared_addons_digest"}
        )
        self.store.write_artifact_manifest(artifact)
        self.store.write_release_tuple_record(
            release.model_copy(update={"artifact_id": artifact.artifact_id})
        )
        self.review = self.review.model_copy(
            update={
                "checklist": checklist.model_copy(
                    update={
                        "candidate": checklist.candidate.model_copy(update=update),
                        "database_compatibility": checklist.database_compatibility.model_copy(
                            update={
                                "candidate_artifact_id": update.get(
                                    "artifact_id", checklist.candidate.artifact_id
                                )
                            }
                        )
                        if checklist.database_compatibility is not None
                        else None,
                    }
                )
            }
        )

    def test_complete_release_mentions_client_and_links_review_with_effect(self) -> None:
        self.publish()
        self.assertEqual(len(self.issues), 1)
        body = self.issues[0]["body"]
        visible_lines = [line for line in body.splitlines() if line and not line.startswith("<!--")]
        self.assertIn("Release review", visible_lines[0])
        self.assertIn("is the site working with these changes", visible_lines[0])
        self.assertNotIn("Change review (preview)", body)
        self.assertIn("@site-owner", body)
        self.assertIn(
            "https://launchplane.example.invalid/ui/owner-review?product=example-site", body
        )
        self.assertIn("Accepting starts the release", body)
        self.assertIn("verified backup", body)
        self.assertIn("check the changes on the testing site", body)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    def test_change_titles_cannot_break_links_or_add_client_mentions(self) -> None:
        self.add_client_change(43)
        self.publish()
        self.change_candidate(source_commit="e" * 40)
        assert self.review.checklist is not None
        checklist = self.review.checklist
        item = checklist.items[-1].model_copy(
            update={
                "title": "Fix ] checkout for @site-owner",
                "owner_test_notes": "Check ] checkout for @site-owner",
            }
        )
        self.review = self.review.model_copy(
            update={"checklist": checklist.model_copy(update={"items": (item,)})}
        )
        self.publish()
        body = self.issues[-1]["body"]
        self.assertIn(f"[#{item.pull_request_number}]({item.url})", body)
        self.assertIn("Check ] checkout", body)
        self.assertEqual(body.count("@site-owner"), 1)
        self.assertEqual(len(self.issues), 2)

    def test_decision_during_lookup_does_not_update_or_remind(self) -> None:
        self.publish()
        self.now += timedelta(days=3)
        original = self.github

        def record_decision(**kwargs: Any) -> object:
            if "/comments?" in kwargs["path"]:
                self.store.write_release_review_decision_record(
                    decision(self.store, date=self.now.isoformat())
                )
            return original(**kwargs)

        with patch("control_plane.release_invitation.github_api_request", record_decision):
            self.publish()
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(self.edits, [])

    def test_lost_reminder_response_is_recovered_without_duplicate(self) -> None:
        self.publish()
        self.now += timedelta(days=3)
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 1)

    def test_no_invitation_when_more_than_client_approval_is_left(self) -> None:
        ready = self.review
        assert ready.checklist is not None
        item = ready.checklist.items[0].model_copy(update={"owner_test_notes": ""})
        for update in (
            {"checklist_complete": False},
            {"checklist": None},
            {"unavailable_reason": "github_read_failed"},
            {"blockers": ("Backup policy missing",)},
            {"approved": True},
            {"required": False},
            {"latest_decision": decision(self.store)},
            {"checklist": ready.checklist.model_copy(update={"items": (item,)})},
            {"checklist": ready.checklist.model_copy(update={"untracked_commits": ("d" * 40,)})},
            {
                "checklist": ready.checklist.model_copy(
                    update={"additional_changes": ("Unknown shared change",)}
                )
            },
            {
                "checklist": ready.checklist.model_copy(
                    update={"candidate": ready.checklist.production}
                )
            },
        ):
            with self.subTest(update=update):
                self.review = ready.model_copy(update=update)
                self.publish()
                self.assertEqual(self.posts, [])

    def test_lost_post_response_is_recovered_without_duplicate(self) -> None:
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish()
        self.assertEqual(len(self.issues), 1)

    def test_concurrent_workers_post_one_comment(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.publish) for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(len(self.issues), 1)

    def test_changed_review_during_lookup_does_not_message(self) -> None:
        ready = self.review
        with patch(
            "control_plane.release_invitation.current_release_review",
            side_effect=[ready, ready.model_copy(update={"approved": True})],
        ):
            self.publish()
        self.assertEqual(self.issues, [])

    def test_held_release_explains_record_only(self) -> None:
        self.profile = profile()
        self.store.write_product_profile_record(self.profile)
        self.publish()
        body = self.issues[0]["body"]
        self.assertIn("**Release review**", body)
        self.assertIn("Accepting records your approval; an admin starts the release", body)
        self.assertNotIn("Accepting starts the release", body)

    def test_effect_matches_review_for_unsupported_driver_and_drill(self) -> None:
        self.profile = self.profile.model_copy(update={"driver_id": "verireel"})
        self.store.write_product_profile_record(self.profile)
        assert self.review.checklist is not None
        with (
            patch(
                "control_plane.release_invitation.release_version",
                side_effect=lambda *, instance, **kwargs: getattr(
                    self.review.checklist, "production" if instance == "prod" else "candidate"
                ),
            ),
            patch(
                "tests.test_release_invitation.release_version",
                side_effect=lambda *, instance, **kwargs: getattr(
                    self.review.checklist, "production" if instance == "prod" else "candidate"
                ),
            ),
        ):
            self.publish()
        self.assertIn("an admin starts the release", self.issues[0]["body"])
        self.issues.clear()
        self.profile = profile().model_copy(
            update={"release_on_acceptance": "promote_with_rollback_drill"}
        )
        self.store.write_product_profile_record(self.profile)
        self.publish()
        self.assertIn("rollback-and-re-release drill", self.issues[0]["body"])

    def test_no_client_prelaunch_retired_and_standing_acceptance_do_not_message(self) -> None:
        for update in (
            {"production_use": "prelaunch"},
            {"lifecycle_state": "retired"},
            {"release_on_acceptance": "director_standing"},
            {"owner": self.profile.owner.model_copy(update={"github_id": "", "github_login": ""})},
        ):
            with self.subTest(update=update):
                self.profile = profile().model_copy(update=update)
                self.publish()
                self.assertEqual(self.posts, [])

    def test_missing_access_and_public_origin_refuse_post(self) -> None:
        with patch(
            "control_plane.release_invitation.resolve_launchplane_github_token", return_value=""
        ):
            with self.assertRaises(ValueError):
                self.publish()
        with patch(
            "control_plane.release_invitation.launchplane_public_origin_from_env", return_value=""
        ):
            self.publish()
        self.assertEqual(self.posts, [])

    def test_worker_read_backoff_is_not_notification_authority(self) -> None:
        backoff = ReleaseInvitationBackoff()
        self.publish(backoff)
        count = self.read_count
        self.publish(backoff)
        self.assertEqual(self.read_count, count)
        self.publish(ReleaseInvitationBackoff())
        self.assertGreater(self.read_count, count)
        self.assertEqual(len(self.issues), 1)

    def test_confirmed_receipt_skips_github_until_candidate_changes(self) -> None:
        backoff = ReleaseInvitationBackoff()
        self.publish(backoff)
        reads = self.read_count
        self.now += timedelta(seconds=301)
        self.publish(backoff)
        self.assertEqual(self.read_count, reads)
        original = self.store.read_artifact_manifest("artifact-testing")
        changed = original.model_copy(
            update={"artifact_id": "new-candidate", "source_commit": "e" * 40}
        )
        self.store.write_artifact_manifest(changed)
        release = self.store.read_release_tuple_record(
            context_name="example-site", channel_name="testing"
        )
        self.store.write_release_tuple_record(
            release.model_copy(update={"artifact_id": changed.artifact_id})
        )
        assert self.review.checklist is not None
        checklist = self.review.checklist
        self.review = self.review.model_copy(
            update={
                "checklist": checklist.model_copy(
                    update={
                        "candidate": checklist.candidate.model_copy(
                            update={
                                "artifact_id": changed.artifact_id,
                                "source_commit": changed.source_commit,
                            }
                        )
                    }
                )
            }
        )
        self.publish(backoff)
        self.assertGreater(self.read_count, reads)
        self.assertEqual(len(self.issues), 2)
        self.assertEqual(len(self.edits), 1)

    def test_marker_command_reads_exact_candidate_without_publishing(self) -> None:
        assert self.review.checklist is not None
        candidate = self.review.checklist.candidate
        candidate_file = self.root / "candidate.json"
        candidate_file.write_text(candidate.model_dump_json())
        output = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "markers",
                    "--product",
                    self.profile.product,
                    "--candidate-file",
                    str(candidate_file),
                ],
            ),
            redirect_stdout(output),
        ):
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["issue_marker"], release_request_issue_marker(self.profile.product))
        self.assertEqual(
            result["comment_marker"], release_invitation_marker(self.profile.product, candidate)
        )
        self.assertEqual(self.posts, [])

    def save_decision(
        self, outcome: str = "accepted", release_start: str = "promote"
    ) -> ReleaseReviewDecisionRecord:
        from control_plane.release_review_record import publish_release_decision

        assert self.review.checklist is not None
        self.now += timedelta(seconds=1)
        saved = decision(self.store, date=self.now.isoformat()).model_copy(
            update={
                "checklist": self.review.checklist,
                "checklist_digest": self.review.checklist_digest,
                "decision": outcome,
                "reason": "Recorded review" if outcome != "accepted" else "",
                "release_issue_url": "",
                "release_start": release_start,
                "actor_github_id": "7001"
                if outcome == "temporary_hotfix"
                else self.profile.owner.github_id,
            }
        )
        self.store.create_release_review_decision_record_if_absent(saved)
        with (
            patch("control_plane.release_review_record.github_api_request", self.github),
            patch(
                "control_plane.release_review_record.resolve_launchplane_github_token",
                return_value="delivery-token",
            ),
            patch(
                "control_plane.release_review_record.resolve_delivery_github_app_id",
                return_value=77,
            ),
            patch(
                "control_plane.release_review_record.launchplane_public_origin_from_env",
                return_value=self.public_origin,
            ),
        ):
            publish_release_decision(
                store=self.store, control_plane_root=self.root, profile=self.profile, decision=saved
            )
        return self.store.read_release_review_decision_record(
            product=self.profile.product, record_id=saved.record_id
        )

    def test_replacement_closes_only_undecided_candidate_and_preserves_request(self) -> None:
        self.publish()
        first = self.issues[0]
        original = first["body"]
        self.change_candidate(artifact_id="next", source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.issues), 2)
        self.assertEqual(first["state"], "closed")
        self.assertTrue(first["body"].startswith(original))
        self.assertIn(
            f"https://github.com/{self.profile.repository}/issues/{self.issues[1]['number']}",
            first["body"],
        )
        self.assertIn("@site-owner", self.issues[1]["body"])
        saved = self.save_decision()
        self.change_candidate(artifact_id="later", source_commit="f" * 40)
        self.publish()
        self.assertEqual(self.issues[1]["state"], "open")
        self.assertIn(f"/issues/{self.issues[1]['number']}", saved.release_issue_url)

    def test_legacy_pending_request_moves_without_a_second_mention_or_decision(self) -> None:
        assert self.review.checklist is not None
        original = {
            "id": 600,
            "created_at": (self.now - timedelta(days=2)).isoformat(),
            "body": _request_marker(self.profile, None)
            + "\n"
            + release_invitation_marker(self.profile.product, self.review.checklist.candidate)
            + "\nHi @site-owner, check the two billing fixes.\n"
            + self.public_origin
            + "/ui/owner-review?product=example-site",
        }
        legacy = {
            "number": 91,
            "state": "open",
            "body": release_request_issue_marker(self.profile.product),
        }
        self.issues.append(legacy)
        self.comments.append(original.copy())
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        for _ in range(2):
            self.assertTrue(self.publish(ReleaseInvitationBackoff()))
        self.assertEqual(len(self.issues), 2)
        carried = self.issues[1]
        self.assertIn("two billing fixes", carried["body"])
        self.assertIn("#issuecomment-600", carried["body"])
        self.assertNotIn("@site-owner", carried["body"])
        self.assertEqual(self.comments, [original])
        self.assertEqual(legacy["state"], "closed")
        self.assertIn(f"/issues/{carried['number']}", str(legacy["body"]))
        self.assertEqual(
            self.store.list_release_review_decision_records(product=self.profile.product), ()
        )
        self.now += timedelta(days=1)
        self.publish(ReleaseInvitationBackoff())
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 2)
        self.assertIn("@site-owner", self.comments[-1]["body"])
        self.assertIn(f"/issues/{carried['number']}", self.save_decision().release_issue_url)
        self.assertEqual(len(self.issues), 2)

    def test_legacy_reminder_receipt_survives_carryover(self) -> None:
        assert self.review.checklist is not None
        request_marker = _request_marker(self.profile, None)
        body = (
            request_marker
            + "\n"
            + release_invitation_marker(self.profile.product, self.review.checklist.candidate)
        )
        self.issues.append(
            {
                "number": 91,
                "state": "open",
                "body": release_request_issue_marker(self.profile.product),
            }
        )
        self.comments.extend(
            [
                {"id": 600, "created_at": (self.now - timedelta(days=5)).isoformat(), "body": body},
                {
                    "id": 601,
                    "created_at": (self.now - timedelta(days=2)).isoformat(),
                    "body": body
                    + "\n"
                    + request_marker.replace("release-request:", "release-reminder:"),
                },
            ]
        )
        original = self.comments.copy()
        self.publish(ReleaseInvitationBackoff())
        self.now += timedelta(days=5)
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(self.comments, original)
        self.assertEqual(len(self.issues), 2)

    def test_rejection_and_hotfix_decision_reuse_the_request_issue(self) -> None:
        self.publish()
        rejected = self.save_decision("changes_requested", "")
        self.assertEqual(self.issues[0]["state"], "closed")
        self.assertEqual(len(self.issues), 1)
        self.assertIn("changes requested", self.issues[0]["body"])
        self.change_candidate(artifact_id="fixed", source_commit="e" * 40)
        self.publish()
        hotfix = self.save_decision("temporary_hotfix")
        self.assertNotEqual(hotfix.release_issue_url, rejected.release_issue_url)
        self.assertIn("@site-owner", self.issues[1]["body"])
        accepted = self.save_decision(release_start="")
        self.assertEqual(accepted.release_issue_url, hotfix.release_issue_url)
        self.assertEqual(self.issues[1]["state"], "closed")
        self.assertEqual(len(self.issues), 2)

    def test_copied_candidate_marker_and_unrelated_empty_bodies_cannot_redirect_decision(
        self,
    ) -> None:
        self.publish()
        original = self.issues.pop()
        self.issues.extend(
            [
                {**original, "number": 89, "performed_via_github_app": {"id": 88}},
                {"number": 90, "body": None},
            ]
        )
        self.publish(ReleaseInvitationBackoff())
        real = self.issues[-1]
        self.assertNotEqual(real["number"], 89)
        self.assertIn(f"/issues/{real['number']}", self.save_decision().release_issue_url)
        self.assertEqual(self.issues[0]["state"], "open")

    def test_closed_shared_history_gets_a_pointer_and_pending_request_stays_open(self) -> None:
        from control_plane.release_invitation import _retire_decided_legacy_requests

        older = decision(self.store, date=(self.now - timedelta(days=3)).isoformat())
        self.store.create_release_review_decision_record_if_absent(older)
        shared = {
            "number": 91,
            "state": "closed",
            "body": release_request_issue_marker(self.profile.product),
        }
        self.issues.append(shared)
        _retire_decided_legacy_requests(
            store=self.store, profile=self.profile, control_plane_root=self.root, backoff=None
        )
        self.assertEqual(shared["state"], "closed")
        self.assertIn(older.release_issue_url, str(shared["body"]))
        self.assertEqual(self.comments, [])
        pending = {
            "number": 92,
            "state": "open",
            "body": release_request_issue_marker(self.profile.product),
        }
        self.issues.append(pending)
        self.comments.append(
            {
                "id": 600,
                "issue_number": 92,
                "created_at": self.now.isoformat(),
                "body": _request_marker(self.profile, older)
                + "\n"
                + release_invitation_marker(self.profile.product, older.checklist.candidate),
            }
        )
        _retire_decided_legacy_requests(
            store=self.store, profile=self.profile, control_plane_root=self.root, backoff=None
        )
        self.assertEqual(pending["state"], "open")
        self.assertEqual(len(self.issues), 2)

    def test_temporary_hotfix_without_ordinary_request_uses_one_issue_for_client_review(
        self,
    ) -> None:
        hotfix = self.save_decision("temporary_hotfix")
        self.assertEqual(len(self.issues), 1)
        self.assertIn("@site-owner", self.issues[0]["body"])
        self.assertIn(self.public_origin + "/ui/owner-review", self.issues[0]["body"])
        self.assertEqual(self.issues[0]["state"], "open")
        rejected = self.save_decision("changes_requested", "rollback")
        self.assertEqual(rejected.release_issue_url, hotfix.release_issue_url)
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(self.issues[0]["state"], "open")
