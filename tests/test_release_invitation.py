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

from control_plane.contracts.release_review import ReleaseReviewStatus
from control_plane.release_invitation import (
    ReleaseInvitationBackoff,
    main,
    publish_release_invitation,
    release_invitation_marker,
    release_request_issue_marker,
)
from control_plane.release_review import ReleaseReviewStore, build_release_review
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
        self.assertIn("@site-owner", body)
        self.assertIn(
            "https://launchplane.example.invalid/ui/owner-review?product=example-site", body
        )
        self.assertIn("Accepting starts the release", body)
        self.assertIn("verified backup", body)
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
        self.assertIn(self.review.checklist.items[0].title, self.comments[0]["body"])
        self.assertEqual(len(self.issues), 1)

    def test_client_decision_then_new_candidate_opens_new_request(self) -> None:
        self.publish()
        for index, outcome in enumerate(("accepted", "changes_requested")):
            with self.subTest(outcome=outcome):
                assert self.review.checklist is not None
                record = decision(
                    self.store, outcome=outcome, date=self.now.isoformat()
                ).model_copy(update={"checklist": self.review.checklist})
                self.store.write_release_review_decision_record(record)
                self.now += timedelta(seconds=1)
                self.change_candidate(source_commit=str(index + 4) * 40)
                self.publish()
                self.publish()
                self.assertEqual(len(self.comments), index + 2)
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
        self.publish()
        self.change_candidate(source_commit="e" * 40)
        assert self.review.checklist is not None
        checklist = self.review.checklist
        item = checklist.items[0].model_copy(update={"title": "Fix ] checkout for @site-owner"})
        self.review = self.review.model_copy(
            update={"checklist": checklist.model_copy(update={"items": (item,)})}
        )
        self.publish()
        body = self.comments[0]["body"]
        self.assertIn(f"[#{item.pull_request_number}]({item.url})", body)
        self.assertIn("Fix ] checkout", body)
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
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(self.posts, [])
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
        self.assertIn("an admin starts the release", self.comments[0]["body"])

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
