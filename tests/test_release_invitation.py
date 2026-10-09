from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.release_review import ReleaseReviewItem, ReleaseReviewStatus
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.release_invitation import (
    ReleaseInvitationBackoff,
    main,
    publish_release_invitation,
    release_invitation_marker,
    release_request_issue_marker,
)
from control_plane.release_review import ReleaseReviewStore, build_release_review
from control_plane.release_invitation_changes import ReleaseInvitationNotesUnavailable
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_release_review import decision, github_read, profile, seed


class ReleaseInvitationTests(unittest.TestCase):
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
        for target, replacement in (
            ("_now", lambda: self.now),
            ("monotonic", lambda: self.now.timestamp()),
            ("launchplane_public_origin_from_env", lambda: "https://launchplane.example.invalid"),
            ("current_release_review", self.read_review),
            ("resolve_launchplane_github_token", lambda **kwargs: "delivery-token"),
            ("github_api_request", self.github),
        ):
            patcher = patch(f"control_plane.release_invitation.{target}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_client_change_batch_notifies_once_and_replaces_previous_invitation(self) -> None:
        self.publish()
        self.add_client_change(43)
        self.add_client_change(44, request_only=True)
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 2)
        self.assertIn("Replaced", self.comments[0]["body"])
        self.assertNotIn("@site-owner", self.comments[0]["body"])
        current = self.comments[1]["body"]
        self.assertIn("@site-owner", current)
        self.assertIn("change 43", current)
        self.assertIn("change 44", current)
        self.assertNotIn("Engineering title", current)
        self.assertNotIn("Update repair prices", current)
        self.assertIn("**Accept**", current)
        self.assertIn("**Request changes**", current)
        self.assertIn("Updated at:", current)
        self.assertEqual(sum("@site-owner" in post.get("body", "") for post in self.posts), 2)

    def test_engineering_landings_after_client_change_produce_zero_new_pings(self) -> None:
        self.add_client_change(43)
        self.publish()
        posts = len(self.posts)
        for sha in ("e" * 40, "f" * 40):
            self.change_candidate(source_commit=sha)
            self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.posts), posts)
        self.assertEqual(len(self.comments), 1)
        self.assertNotIn("@site-owner", self.comments[0]["body"])
        self.assertIn("change 43", self.comments[0]["body"])

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
        self.assertEqual(len(self.comments), 1)
        self.assertNotIn("@site-owner", self.comments[0]["body"])
        self.assertNotIn("change 43", self.comments[0]["body"])
        self.assertNotIn("change 44", self.comments[0]["body"])

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
        self.assertEqual(len(self.comments), 2)
        self.assertIn("change 43", self.comments[-1]["body"])

    def seed_train_batch(self) -> ReleaseReviewItem:
        self.add_client_change(43)
        assert self.review.checklist is not None
        batch = self.review.checklist.items[0].model_copy(
            update={
                "pull_request_number": 99,
                "url": "https://github.com/example/site/pull/99",
                "owner_test_notes": "### #43 Prices\n\nCheck the new repair prices for change 43.",
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
                for position, number in enumerate((42, 43), start=1)
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

    def test_legacy_batch_receipt_is_adopted_without_another_ping(self) -> None:
        batch = self.seed_train_batch()
        assert self.review.checklist is not None
        self.issues = [{"number": 91, "body": release_request_issue_marker(self.profile.product)}]
        self.comments = [
            {
                "id": 1,
                "created_at": self.now.isoformat(),
                "body": release_invitation_marker(
                    self.profile.product, self.review.checklist.candidate
                )
                + f"\n\n@site-owner [#99]({batch.url}): Prices",
            }
        ]
        self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.posts, [])
        self.assertIn("pull/43", self.comments[0]["body"])
        self.assertNotIn("@site-owner", self.comments[0]["body"])

    def test_train_batch_uses_constituent_review_evidence_and_only_its_notes(self) -> None:
        self.publish()
        batch = self.seed_train_batch()
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.comments), 2)
        self.assertIn("change 43", self.comments[-1]["body"])
        self.assertIn("https://github.com/example/site/pull/43", self.comments[-1]["body"])
        self.assertNotIn("No manual check needed", self.comments[-1]["body"])
        self.assertNotIn("pull/99", self.comments[-1]["body"])
        broken = batch.model_copy(
            update={"owner_test_notes": "### #42 Engineering\n\nNo manual check needed."}
        )
        assert self.review.checklist is not None
        self.review = self.review.model_copy(
            update={"checklist": self.review.checklist.model_copy(update={"items": (broken,)})}
        )
        self.change_candidate(source_commit="f" * 40)
        with self.assertRaisesRegex(ReleaseInvitationNotesUnavailable, "#43"):
            self.publish()
        self.assertEqual(len(self.comments), 2)

    def test_lost_material_post_and_replacement_responses_recover_without_ping(self) -> None:
        self.publish()
        self.add_client_change(43)
        self.change_candidate(source_commit="e" * 40)
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 2)
        self.assertEqual(sum("@site-owner" in post.get("body", "") for post in self.posts), 2)
        self.assertIn("Replaced", self.comments[0]["body"])

    def test_concurrent_material_update_is_one_notification(self) -> None:
        self.publish()
        self.add_client_change(43)
        self.change_candidate(source_commit="e" * 40)
        with ThreadPoolExecutor(max_workers=2) as executor:
            for future in [executor.submit(self.publish) for _ in range(2)]:
                future.result()
        self.assertEqual(len(self.comments), 2)
        self.assertIn("Replaced", self.comments[0]["body"])

    def test_removed_and_returning_client_change_is_not_announced_again(self) -> None:
        self.add_client_change(43)
        self.publish()
        assert self.review.checklist is not None
        original = self.review.checklist.items
        for sha, items in (("e" * 40, original[:1]), ("f" * 40, original)):
            self.change_candidate(source_commit=sha)
            self.review = self.review.model_copy(
                update={"checklist": self.review.checklist.model_copy(update={"items": items})}
            )
            self.publish()
        self.assertEqual(len(self.comments), 1)

    def read_review(self, **_kwargs: Any) -> ReleaseReviewStatus:
        self.read_count += 1
        return self.review

    def github(self, *, path: str, token: str, **kwargs: Any) -> object:
        self.assertEqual(token, "delivery-token")
        if path == "/installation/token" and kwargs.get("method") == "DELETE":
            return None
        self.assertTrue(path.startswith("/repos/example/site/issues"))
        if kwargs.get("method") == "PATCH":
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
                result = {"id": len(self.comments) + 1, "created_at": self.now.isoformat(), **body}
                self.comments.append(result)
                if self.lose_response:
                    self.lose_response = False
                    raise TimeoutError("response lost after publication")
                return result
            issue = {"number": 91, **body}
            self.issues.append(issue)
            return issue
        return self.comments.copy() if "/comments?" in path else self.issues.copy()

    def publish(self, backoff: ReleaseInvitationBackoff | None = None) -> None:
        publish_release_invitation(
            store=cast(ReleaseReviewStore, self.store),
            control_plane_root=self.root,
            profile=self.profile,
            backoff=backoff,
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
                    update={"candidate": checklist.candidate.model_copy(update=update)}
                )
            }
        )

    def test_complete_release_mentions_client_and_links_review_with_effect(self) -> None:
        self.publish()
        self.assertEqual(len(self.comments), 1)
        body = self.comments[0]["body"]
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
        self.assertIn("there is nothing new to test", body)
        self.assertEqual(
            self.store.list_release_review_decision_records(product="example-site"), ()
        )

    def test_only_once_even_after_notes_change_or_worker_restart(self) -> None:
        self.publish()
        self.review = self.review.model_copy(update={"checklist_digest": "f" * 64})
        self.publish()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.issues), 1)

    def test_new_artifact_commit_or_shared_inputs_update_open_request_without_mention(self) -> None:
        self.publish()
        for update in (
            {"artifact_id": "replacement-artifact"},
            {"source_commit": "e" * 40},
            {"shared_addons_digest": "changed-shared-inputs"},
        ):
            self.change_candidate(**update)
            self.publish()
            self.publish()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.edits), 3)
        self.assertEqual(sum("@site-owner" in post.get("body", "") for post in self.posts), 1)
        self.assertTrue(all("@site-owner" not in edit["body"] for edit in self.edits))
        assert self.review.checklist is not None
        self.assertIn(
            release_invitation_marker(self.profile.product, self.review.checklist.candidate),
            self.comments[0]["body"],
        )
        self.assertNotIn(self.review.checklist.items[0].title, self.comments[0]["body"])
        self.assertEqual(len(self.issues), 1)

    def test_client_decision_then_new_candidate_opens_new_request(self) -> None:
        self.publish()
        for index, outcome in enumerate(("accepted", "changes_requested")):
            with self.subTest(outcome=outcome):
                assert self.review.checklist is not None
                record = decision(
                    self.store, outcome=outcome, date=self.now.isoformat()
                ).model_copy(update={"checklist": self.review.checklist})
                decided_invitation = self.comments[index]["body"]
                self.store.write_release_review_decision_record(record)
                self.now += timedelta(seconds=1)
                self.change_candidate(source_commit=str(index + 4) * 40)
                self.publish()
                self.publish()
                self.assertEqual(len(self.comments), index + 2)
                self.assertEqual(self.comments[index]["body"], decided_invitation)
                self.assertEqual(
                    sum("@site-owner" in post.get("body", "") for post in self.posts), index + 2
                )

    def test_lost_edit_response_and_restart_do_not_post_again(self) -> None:
        self.publish()
        self.change_candidate(source_commit="e" * 40)
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.edits), 1)

    def test_returning_to_earlier_candidate_updates_cached_open_request(self) -> None:
        assert self.review.checklist is not None
        original = self.review.checklist.candidate.source_commit
        original_marker = release_invitation_marker(
            self.profile.product, self.review.checklist.candidate
        )
        backoff = ReleaseInvitationBackoff()
        self.publish(backoff)
        self.change_candidate(source_commit="e" * 40)
        self.publish(backoff)
        self.change_candidate(source_commit=original)
        self.publish(backoff)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.edits), 2)
        self.assertIn(original_marker, self.comments[0]["body"])

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
        body = self.comments[0]["body"]
        self.assertIn(f"[#{item.pull_request_number}]({item.url})", body)
        self.assertIn("Check ] checkout", body)
        self.assertNotIn("@site-owner", body)
        self.assertEqual(len(self.comments), 1)

    def test_admin_override_keeps_client_request_open_without_a_new_mention(self) -> None:
        self.publish()
        self.store.write_release_review_decision_record(
            decision(self.store, outcome="overridden", date=self.now.isoformat())
        )
        self.change_candidate(source_commit="e" * 40)
        self.publish()
        self.assertEqual(len(self.comments), 1)
        self.assertNotIn("@site-owner", self.edits[0]["body"])

    def test_notes_line_separators_cannot_supply_a_reminder_receipt(self) -> None:
        self.add_client_change(43)
        self.publish()
        request_marker = self.comments[0]["body"].splitlines()[0]
        reminder_marker = request_marker.replace("release-request:", "release-reminder:")
        self.change_candidate(source_commit="e" * 40)
        assert self.review.checklist is not None
        checklist = self.review.checklist
        item = checklist.items[-1].model_copy(
            update={"owner_test_notes": f"Check\u2028{reminder_marker}\u2029continued"}
        )
        self.review = self.review.model_copy(
            update={"checklist": checklist.model_copy(update={"items": (item,)})}
        )
        self.publish()
        self.now += timedelta(days=3)
        self.publish()
        self.assertEqual(len(self.comments), 2)
        self.assertIn("@site-owner", self.comments[1]["body"])

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
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.edits, [])

    def test_one_reminder_after_three_days_even_across_candidates_and_restarts(self) -> None:
        self.publish()
        backoff = ReleaseInvitationBackoff()
        self.now += timedelta(days=3) - timedelta(seconds=1)
        self.change_candidate(source_commit="e" * 40)
        self.publish(backoff)
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 1)
        self.now += timedelta(seconds=1)
        # The cached receipt expires at the original request's deadline.
        self.publish(backoff)
        self.assertEqual(len(self.comments), 2)
        self.assertIn("@site-owner", self.comments[1]["body"])
        self.now += timedelta(days=10)
        self.change_candidate(source_commit="f" * 40)
        self.publish(ReleaseInvitationBackoff())
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 2)

    def test_lost_reminder_response_is_recovered_without_duplicate(self) -> None:
        self.publish()
        self.now += timedelta(days=3)
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(len(self.comments), 2)

    def test_newest_legacy_open_request_is_updated_without_mention(self) -> None:
        assert self.review.checklist is not None
        candidate = self.review.checklist.candidate
        self.issues = [{"number": 91, "body": release_request_issue_marker(self.profile.product)}]
        self.comments = [
            {
                "id": index + 1,
                "created_at": self.now.isoformat(),
                "body": release_invitation_marker(
                    self.profile.product,
                    candidate.model_copy(update={"source_commit": str(index) * 40}),
                )
                + "\n\n@site-owner old request",
            }
            for index in range(3)
        ]
        self.publish()
        self.publish()
        self.assertEqual(len(self.comments), 3)
        self.assertEqual(len(self.edits), 3)
        self.assertEqual(self.posts, [])
        self.assertTrue(all("Replaced" in comment["body"] for comment in self.comments[:-1]))
        self.assertTrue(all("old request" in comment["body"] for comment in self.comments[:-1]))
        self.assertIn(
            release_invitation_marker(self.profile.product, candidate), self.comments[-1]["body"]
        )

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

    def test_manual_request_is_adopted_without_another_mention(self) -> None:
        assert self.review.checklist is not None
        self.issues = [
            {"number": 91, "body": "Go live\n" + release_request_issue_marker(self.profile.product)}
        ]
        self.comments = [
            {
                "id": 1,
                "created_at": self.now.isoformat(),
                "body": "Existing manual request\n"
                + release_invitation_marker(self.profile.product, self.review.checklist.candidate),
            }
        ]
        self.publish()
        self.assertEqual(self.posts, [])

    def test_lost_post_response_is_recovered_without_duplicate(self) -> None:
        self.lose_response = True
        with self.assertRaises(TimeoutError):
            self.publish()
        self.publish()
        self.assertEqual(len(self.comments), 1)

    def test_concurrent_workers_post_one_comment(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.publish) for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.issues), 1)

    def test_changed_review_during_lookup_does_not_message(self) -> None:
        ready = self.review
        with patch(
            "control_plane.release_invitation.current_release_review",
            side_effect=[ready, ready.model_copy(update={"approved": True})],
        ):
            self.publish()
        self.assertEqual(self.comments, [])

    def test_held_release_explains_record_only(self) -> None:
        self.profile = profile()
        self.store.write_product_profile_record(self.profile)
        self.publish()
        body = self.comments[0]["body"]
        self.assertIn("**Release review**", body)
        self.assertIn("Accepting records your approval; an admin starts the release", body)
        self.assertNotIn("Accepting starts the release", body)

    def test_effect_matches_review_for_unsupported_driver_and_drill(self) -> None:
        self.profile = self.profile.model_copy(update={"driver_id": "verireel"})
        self.store.write_product_profile_record(self.profile)
        assert self.review.checklist is not None
        with patch(
            "control_plane.release_invitation.release_version",
            side_effect=[self.review.checklist.production, self.review.checklist.candidate],
        ):
            self.publish()
        self.assertIn("an admin starts the release", self.comments[0]["body"])
        self.comments.clear()
        self.profile = profile().model_copy(
            update={"release_on_acceptance": "promote_with_rollback_drill"}
        )
        self.store.write_product_profile_record(self.profile)
        self.publish()
        self.assertIn("rollback-and-re-release drill", self.comments[0]["body"])

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

    def test_ambiguous_or_unreadable_destination_refuses_post(self) -> None:
        marker = release_request_issue_marker(self.profile.product)
        self.issues = [{"number": number, "body": marker} for number in (1, 2)]
        with self.assertRaises(ValueError):
            self.publish()
        with patch("control_plane.release_invitation.github_api_request", return_value={}):
            with self.assertRaises(ValueError):
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
        self.assertEqual(len(self.comments), 1)

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
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.edits), 1)

    def test_settled_candidate_needs_no_github_read(self) -> None:
        self.store.create_release_review_decision_record_if_absent(decision(self.store))
        self.publish(ReleaseInvitationBackoff())
        self.assertEqual(self.read_count, 0)
        self.assertEqual(self.posts, [])

    def test_large_repository_stops_after_finding_marked_issue(self) -> None:
        marker = release_request_issue_marker(self.profile.product)
        original = self.github
        pages: list[str] = []

        def paged(**kwargs: Any) -> object:
            path = kwargs["path"]
            if kwargs.get("method") == "POST" or "/comments?" in path:
                return original(**kwargs)
            pages.append(path)
            if path.endswith("page=1"):
                return [
                    {"number": number, "pull_request": {}, "body": marker} for number in range(100)
                ]
            if path.endswith("page=2"):
                return [{"number": 91, "body": marker}] + [
                    {"number": number, "body": "Old issue"} for number in range(99)
                ]
            raise AssertionError("Lookup continued after the destination was found")

        with patch("control_plane.release_invitation.github_api_request", paged):
            self.publish()
        self.assertEqual(len([page for page in pages if page.startswith("/repos/")]), 2)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.posts), 1)

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
