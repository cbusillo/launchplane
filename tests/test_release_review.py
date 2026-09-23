import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import cast

import click

from control_plane.contracts.artifact_identity import (
    ArtifactAddonSource,
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductOwnerProfile,
    ProductLaneProfile,
)
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord, ReleaseReviewStatus
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.release_review import build_release_review, require_release_approval
from control_plane.release_review_github import owner_test_notes, read_release_changes
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import sqlite_database_url

BASE = "a" * 40
HEAD = "b" * 40
PR_HEAD = "c" * 40


def profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="example-site",
        display_name="Example site",
        repository="example/site",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/example/site"),
        owner=ProductOwnerProfile(github_id="9001", github_login="site-owner"),
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="example-site",
                base_url="https://testing.example.invalid",
            ),
            ProductLaneProfile(instance="prod", context="example-site"),
        ),
        production_use="live",
        updated_at="2026-09-23T00:00:00Z",
        source="test",
    )


def seed(store: FilesystemRecordStore | PostgresRecordStore) -> None:
    store.write_product_profile_record(profile())
    for instance, sha in (("prod", BASE), ("testing", HEAD)):
        artifact_id = f"artifact-{instance}"
        store.write_artifact_manifest(
            ArtifactIdentityManifest(
                artifact_id=artifact_id,
                source_commit=sha,
                enterprise_base_digest="sha256:base",
                image=ArtifactImageReference(
                    repository="ghcr.io/example/site", digest="sha256:" + sha
                ),
            )
        )
        store.write_release_tuple_record(
            ReleaseTupleRecord(
                tuple_id=f"tuple-{instance}",
                context="example-site",
                channel=instance,
                artifact_id=artifact_id,
                repo_shas={"example/site": sha},
                provenance="ship",
                minted_at="2026-09-23T00:00:00Z",
            )
        )


def github_read(path: str) -> object:
    if "/compare/" in path:
        return {"status": "ahead", "total_commits": 1, "commits": [{"sha": HEAD}]}
    return [
        {
            "number": 42,
            "title": "Update repair prices",
            "body": "## Owner test notes\nCheck the repair prices.\n## Validation\nTests pass.",
            "merged_at": "2026-09-23T00:00:00Z",
            "merge_commit_sha": HEAD,
            "head": {"sha": PR_HEAD},
            "base": {"repo": {"full_name": "example/site"}},
        }
    ]


def decision(
    store: FilesystemRecordStore | PostgresRecordStore,
    *,
    outcome: str = "accepted",
    date: str = "2026-09-23T01:00:00Z",
) -> ReleaseReviewDecisionRecord:
    review = build_release_review(store=store, profile=profile(), read=github_read)
    assert review.checklist is not None
    return ReleaseReviewDecisionRecord.model_validate(
        {
            "record_id": f"decision-{date}",
            "product": "example-site",
            "checklist_digest": review.checklist_digest,
            "checklist": review.checklist,
            "decision": outcome,
            "reason": "Recorded reason" if outcome != "accepted" else "",
            "actor_github_id": "9001",
            "actor_github_login": "site-owner",
            "decided_at": date,
            "release_issue_url": "https://github.com/example/site/issues/99",
        }
    )


class ReleaseReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FilesystemRecordStore(Path(self.directory.name))
        seed(self.store)

    def review(self) -> ReleaseReviewStatus:
        return build_release_review(store=self.store, profile=profile(), read=github_read)

    def test_records_release_from_actual_production_and_testing_commits(self) -> None:
        review = self.review()
        assert review.checklist is not None
        self.assertFalse(review.approved)
        self.assertEqual(review.checklist.production.source_commit, BASE)
        self.assertEqual(review.checklist.candidate.source_commit, HEAD)
        self.assertEqual(review.checklist.items[0].owner_test_notes, "Check the repair prices.")
        self.store.write_release_review_decision_record(decision(self.store))
        self.assertTrue(self.review().approved)

    def test_pending_release_record_never_approves_promotion(self) -> None:
        for outcome in ("accepted", "overridden"):
            with self.subTest(outcome=outcome):
                self.store.write_release_review_decision_record(
                    decision(self.store, outcome=outcome).model_copy(
                        update={"release_issue_url": ""}
                    )
                )
                self.assertFalse(self.review().approved)
                self.assertTrue(
                    any("release record" in blocker for blocker in self.review().blockers)
                )

    def test_shared_addon_only_change_is_visible_and_cannot_be_owner_accepted(self) -> None:
        artifact = self.store.read_artifact_manifest("artifact-testing")
        self.store.write_artifact_manifest(
            artifact.model_copy(
                update={
                    "source_commit": BASE,
                    "addon_sources": (
                        ArtifactAddonSource(repository="example/shared-addons", ref=HEAD),
                    ),
                }
            )
        )
        review = build_release_review(
            store=self.store,
            profile=profile(),
            read=lambda path: {
                "status": "identical",
                "total_commits": 0,
                "commits": [],
            },
        )
        assert review.checklist is not None
        self.assertEqual(review.checklist.items, ())
        self.assertTrue(review.checklist.additional_changes)
        self.assertFalse(review.approved)
        self.assertIn("Shared website components", review.blockers[0])

    def test_prelaunch_profile_write_requires_recorded_reason(self) -> None:
        prelaunch = profile().model_copy(update={"production_use": "prelaunch"})
        with self.assertRaisesRegex(ValueError, "classification reason"):
            prelaunch.validate_write_contract()
        prelaunch.model_copy(
            update={"production_use_reason": "Not serving real customers."}
        ).validate_write_contract()

    def test_rejection_supersedes_acceptance_without_deploying(self) -> None:
        self.store.write_release_review_decision_record(decision(self.store))
        self.store.write_release_review_decision_record(
            decision(self.store, outcome="changes_requested", date="2026-09-23T02:00:00Z")
        )
        self.assertFalse(self.review().approved)
        self.assertEqual(self.store.list_deployment_records(), ())

    def test_changed_notes_or_owner_invalidate_approval(self) -> None:
        self.store.write_release_review_decision_record(decision(self.store))

        def changed_notes(path: str) -> object:
            result = github_read(path)
            if isinstance(result, list):
                result[0]["body"] = "## Owner test notes\nCheck booking too."
            return result

        changed = build_release_review(store=self.store, profile=profile(), read=changed_notes)
        self.assertFalse(changed.approved)
        new_owner = profile().model_copy(
            update={"owner": profile().owner.model_copy(update={"github_id": "9002"})}
        )
        self.assertFalse(
            build_release_review(store=self.store, profile=new_owner, read=github_read).approved
        )

    def test_preview_acceptance_is_annotation_not_release_approval(self) -> None:
        self.store.write_product_review_decision_record(
            ProductReviewDecisionRecord(
                record_id="preview-decision",
                product="example-site",
                repository="example/site",
                pull_request_number=42,
                head_sha=PR_HEAD,
                decision="accepted",
                owner_github_id="9001",
                owner_github_login="site-owner",
                decided_at="2026-09-23T00:00:00Z",
            )
        )
        review = self.review()
        assert review.checklist is not None
        self.assertTrue(review.checklist.items[0].already_reviewed)
        self.assertFalse(review.approved)

    def test_missing_notes_and_direct_commits_are_visible_blockers(self) -> None:
        def no_notes(path: str) -> object:
            result = github_read(path)
            if isinstance(result, list):
                result[0]["body"] = "## Summary\nUpdated"
            return result

        review = build_release_review(store=self.store, profile=profile(), read=no_notes)
        self.assertIn("Pull request #42 has no Owner test notes.", review.blockers)
        review = build_release_review(
            store=self.store,
            profile=profile(),
            read=lambda path: github_read(path) if "/compare/" in path else [],
        )
        assert review.checklist is not None
        self.assertEqual(review.checklist.untracked_commits, (HEAD,))
        self.assertFalse(review.approved)

    def test_unknown_production_use_requires_review_prelaunch_is_explicit(self) -> None:
        live = profile().model_copy(update={"production_use": "unknown"})
        self.store.write_product_profile_record(live)
        with (
            self.assertRaises(click.ClickException),
            patch("control_plane.release_review.resolve_launchplane_github_token", return_value=""),
        ):
            require_release_approval(
                control_plane_root=Path(self.directory.name),
                record_store=self.store,
                product=live.product,
            )
        self.store.write_product_profile_record(
            live.model_copy(update={"production_use": "prelaunch"})
        )
        with patch("control_plane.release_review.resolve_launchplane_github_token") as token:
            require_release_approval(
                control_plane_root=Path(self.directory.name),
                record_store=self.store,
                product=live.product,
            )
        token.assert_not_called()

    def test_approval_does_not_authorize_another_artifact(self) -> None:
        self.store.write_release_review_decision_record(decision(self.store))
        with (
            patch(
                "control_plane.release_review.current_release_review", return_value=self.review()
            ),
            self.assertRaisesRegex(click.ClickException, "no longer matches"),
        ):
            require_release_approval(
                control_plane_root=Path(self.directory.name),
                record_store=self.store,
                product="example-site",
                artifact_id="other-artifact",
            )

    def test_odoo_run_stops_before_backup_and_provider_without_release_approval(self) -> None:
        from control_plane.workflows.odoo_prod_promotion_run import (
            OdooProdPromotionRunRequest,
            OdooProdPromotionRunStore,
            execute_odoo_prod_promotion_run,
        )

        with (
            patch("control_plane.release_review.resolve_launchplane_github_token", return_value=""),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate"
            ) as backup,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion"
            ) as promotion,
        ):
            result = execute_odoo_prod_promotion_run(
                control_plane_root=Path(self.directory.name),
                state_dir=Path(self.directory.name),
                database_url=None,
                record_store=cast(OdooProdPromotionRunStore, self.store),
                request=OdooProdPromotionRunRequest(
                    product="example-site", context="example-site", request_id="release-test"
                ),
            )
        self.assertEqual(result.run_status, "blocked")
        backup.assert_not_called()
        promotion.assert_not_called()
        self.assertEqual(self.store.list_deployment_records(), ())

    def test_sqlite_round_trip(self) -> None:
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        )
        store.ensure_schema()
        self.addCleanup(store.close)
        seed(store)
        record = decision(store)
        store.write_release_review_decision_record(
            record.model_copy(update={"release_issue_url": ""})
        )
        self.assertFalse(
            build_release_review(store=store, profile=profile(), read=github_read).approved
        )
        store.write_release_review_decision_record(record)
        self.assertEqual(
            store.list_release_review_decision_records(product="example-site"), (record,)
        )
        self.assertTrue(
            build_release_review(store=store, profile=profile(), read=github_read).approved
        )


class ReleaseGitHubTests(unittest.TestCase):
    def test_ambiguous_notes_are_a_visible_coverage_blocker(self) -> None:
        def ambiguous(path: str) -> object:
            result = github_read(path)
            if isinstance(result, list):
                result[0]["body"] = "## Owner test notes\nFirst\n## Owner test notes\nSecond"
            return result

        items, untracked = read_release_changes(
            repository="example/site", production_commit=BASE, candidate_commit=HEAD, read=ambiguous
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner_test_notes, "")
        self.assertEqual(untracked, ())

    def test_malformed_pull_request_fails_as_incomplete_evidence(self) -> None:
        def malformed(path: str) -> object:
            result = github_read(path)
            if isinstance(result, list):
                del result[0]["number"]
            return result

        with self.assertRaisesRegex(ValueError, "number or title"):
            read_release_changes(
                repository="example/site",
                production_commit=BASE,
                candidate_commit=HEAD,
                read=malformed,
            )

    def test_notes_ignore_fenced_heading_and_preserve_subheadings(self) -> None:
        self.assertEqual(
            owner_test_notes(
                "```\n## Owner test notes\nFake\n```\n## Owner test notes\nReal\n### Phone\nSmall screen\n## Other\nNo"
            ),
            "Real\n### Phone\nSmall screen",
        )
        self.assertEqual(
            owner_test_notes("## Owner test notes\nNothing for the owner to test"),
            "Nothing for the owner to test",
        )

    def test_duplicate_notes_and_incomplete_comparison_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            owner_test_notes("## Owner test notes\nOne\n## Owner test notes\nTwo")
        for comparison in (
            {"status": "diverged", "total_commits": 1, "commits": []},
            {"status": "ahead", "total_commits": 2, "commits": []},
        ):
            with self.subTest(comparison=comparison), self.assertRaises(ValueError):
                read_release_changes(
                    repository="example/site",
                    production_commit=BASE,
                    candidate_commit=HEAD,
                    read=lambda path: comparison,
                )

    def test_comparison_pages_are_all_read_and_pulls_deduplicated(self) -> None:
        commits = [f"{index:040x}" for index in range(1, 102)]

        def read(path: str) -> object:
            if "/compare/" in path:
                batch = commits[:100] if "&page=1" in path else commits[100:]
                return {
                    "status": "ahead",
                    "total_commits": 101,
                    "commits": [{"sha": sha} for sha in batch],
                }
            result = github_read(path)
            assert isinstance(result, list)
            result[0]["merge_commit_sha"] = commits[-1]
            return result

        items, uncovered = read_release_changes(
            repository="example/site",
            production_commit=BASE,
            candidate_commit=commits[-1],
            read=read,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(uncovered, ())
