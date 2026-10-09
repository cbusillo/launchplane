"""Shared-source release coverage, including a website plus two addon repositories."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.artifact_identity import ArtifactAddonSelector, ArtifactAddonSource
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord, ReleaseReviewStatus
from control_plane.release_review import (
    build_release_review,
    checklist_blockers,
    checklist_digest,
    current_release_review,
)
from control_plane.release_review_record import release_decision_issue_body
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_release_review import BASE, HEAD, profile, seed


class SharedGitHub:
    def __init__(self) -> None:
        self.notes = "Check staff sign-in and follow a readable record link."
        self.uncovered = False
        self.fail = False
        self.status = "ahead"

    def read(self, path: str) -> object:
        repository = "/".join(path.split("/")[2:4])
        website = repository == profile().repository
        commits = [f"{number:040x}" for number in range(1, 30)] if website else [HEAD]
        if self.fail and not website:
            raise click.ClickException("private provider error")
        if "/compare/" in path:
            return {
                "status": "ahead" if website else self.status,
                "total_commits": len(commits),
                "commits": [{"sha": sha} for sha in commits],
            }
        sha = path.split("/commits/")[1].split("/")[0]
        if self.uncovered and not website:
            return []
        return [
            {
                "number": int(sha, 16) if website else 1,
                "title": "Website change" if website else "Shared staff sign-in",
                "body": "## Client test notes\n"
                + ("Test the website change." if website else self.notes),
                "merged_at": "2026-10-04T00:00:00Z",
                "merge_commit_sha": sha,
                "head": {"sha": sha},
                "base": {"repo": {"full_name": repository}},
            }
        ]


class SharedReleaseReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = FilesystemRecordStore(self.root)
        seed(self.store)
        self.github = SharedGitHub()
        for instance, sha in (("prod", BASE), ("testing", HEAD)):
            self.set_sources(instance, sha)

    def set_sources(self, instance: str, sha: str) -> None:
        artifact = self.store.read_artifact_manifest(f"artifact-{instance}")
        sources = ("example/shared-addons", "example/disable-online")
        self.store.write_artifact_manifest(
            artifact.model_copy(
                update={
                    "addon_sources": tuple(
                        ArtifactAddonSource(repository=repo, ref=sha) for repo in sources
                    ),
                    "addon_selectors": tuple(
                        ArtifactAddonSelector(repository=repo, selector="main", resolved_ref=sha)
                        for repo in sources
                    ),
                }
            )
        )

    def review(self) -> ReleaseReviewStatus:
        return build_release_review(store=self.store, profile=profile(), read=self.github.read)

    def accept(self) -> ReleaseReviewDecisionRecord:
        review = self.review()
        assert review.checklist is not None
        record = ReleaseReviewDecisionRecord(
            record_id="shared-acceptance",
            product=profile().product,
            checklist_digest=review.checklist_digest,
            checklist=review.checklist,
            decision="accepted",
            actor_github_id="9001",
            actor_github_login="site-owner",
            decided_at="2026-10-05T00:00:00Z",
            release_issue_url="https://github.com/example/site/issues/90",
        )
        self.store.write_release_review_decision_record(record)
        return record

    def test_website_and_two_shared_ranges_are_reviewable_and_persisted(self) -> None:
        review = self.review()
        assert review.checklist is not None
        self.assertEqual(len(review.checklist.items), len(range(1, 30)))
        self.assertEqual(
            {source.repository for source in review.checklist.shared_sources},
            {"example/shared-addons", "example/disable-online"},
        )
        self.assertEqual(checklist_blockers(review.checklist), ())
        for source in review.checklist.shared_sources:
            self.assertEqual((source.production_commit, source.candidate_commit), (BASE, HEAD))
            self.assertEqual(source.items[0].owner_test_notes, self.github.notes)
        record = self.accept()
        self.assertTrue(self.review().approved)
        saved = self.store.list_release_review_decision_records(product=profile().product)[0]
        self.assertEqual(saved, record)
        body = release_decision_issue_body(saved)
        for source in saved.checklist.shared_sources:
            self.assertIn(source.items[0].url, body)
            self.assertIn(source.items[0].owner_test_notes, body)
        self.assertEqual(self.store.list_deployment_records(), ())

    def test_shared_only_changes_still_have_a_checklist(self) -> None:
        artifact = self.store.read_artifact_manifest("artifact-testing")
        self.store.write_artifact_manifest(artifact.model_copy(update={"source_commit": BASE}))

        def read(path: str) -> object:
            if path.startswith("/repos/example/site/compare/"):
                return {"status": "identical", "total_commits": 0, "commits": []}
            return self.github.read(path)

        review = build_release_review(store=self.store, profile=profile(), read=read)
        assert review.checklist is not None
        self.assertEqual(review.checklist.items, ())
        self.assertTrue(review.checklist.shared_sources)
        self.assertEqual(checklist_blockers(review.checklist), ())

    def test_missing_shared_notes_and_batch_notes_or_uncovered_commits_block(self) -> None:
        for notes, uncovered in (
            ("", False),
            ("#52 has no Client test notes.", False),
            (self.github.notes, True),
        ):
            with self.subTest(notes=notes, uncovered=uncovered):
                self.github.notes, self.github.uncovered = notes, uncovered
                self.accept()  # Even an existing accepted record cannot waive coverage.
                review = self.review()
                self.assertFalse(review.approved)
                self.assertTrue(
                    any("example/shared-addons" in blocker for blocker in review.blockers)
                )

    def test_shared_notes_or_candidate_changes_require_new_acceptance(self) -> None:
        self.accept()
        self.github.notes = "Check sign-in with a second staff user."
        self.assertFalse(self.review().approved)
        self.accept()
        self.set_sources("testing", "d" * 40)
        self.assertFalse(self.review().approved)

    def test_added_removed_selected_or_ambiguous_sources_remain_blocked(self) -> None:
        original = self.store.read_artifact_manifest("artifact-testing")
        for updates in (
            {
                "addon_sources": original.addon_sources[:1],
                "addon_selectors": original.addon_selectors[:1],
            },
            {
                "addon_sources": (
                    *original.addon_sources,
                    ArtifactAddonSource(repository="example/new", ref=HEAD),
                )
            },
            {"addon_sources": (*original.addon_sources, original.addon_sources[0])},
            {
                "addon_selectors": (
                    original.addon_selectors[0].model_copy(update={"selector": "other"}),
                    original.addon_selectors[1],
                )
            },
            {
                "addon_selectors": (
                    original.addon_selectors[0].model_copy(update={"resolved_ref": BASE}),
                    original.addon_selectors[1],
                )
            },
        ):
            with self.subTest(updates=updates):
                self.store.write_artifact_manifest(original.model_copy(update=updates))
                review = self.review()
                assert review.checklist is not None
                self.assertTrue(review.checklist.additional_changes)
                self.assertFalse(review.approved)

    def test_repository_forms_normalize_to_the_same_shared_range(self) -> None:
        artifact = self.store.read_artifact_manifest("artifact-prod")
        self.store.write_artifact_manifest(
            artifact.model_copy(
                update={
                    "addon_sources": tuple(
                        source.model_copy(
                            update={"repository": f"git@github.com:{source.repository}.git"}
                        )
                        for source in artifact.addon_sources
                    ),
                    "addon_selectors": tuple(
                        source.model_copy(
                            update={"repository": f"https://github.com/{source.repository}.git"}
                        )
                        for source in artifact.addon_selectors
                    ),
                }
            )
        )
        checklist = self.review().checklist
        assert checklist is not None
        self.assertEqual(checklist.additional_changes, ())

    def test_scoped_access_and_shared_read_failure_fail_closed(self) -> None:
        def resolve_token(**kwargs: object) -> str:
            repository = kwargs["repository"]
            assert isinstance(repository, str)
            return repository

        revoked: list[str] = []

        def request(*, path: str, token: str, method: str = "GET") -> object:
            if path == "/installation/token":
                self.assertEqual(method, "DELETE")
                revoked.append(token)
                return None
            self.assertEqual(token, "/".join(path.split("/")[2:4]))
            return self.github.read(path)

        with (
            patch(
                "control_plane.release_review.resolve_launchplane_github_token",
                side_effect=resolve_token,
            ),
            patch("control_plane.release_review.github_api_request", side_effect=request),
        ):
            review = current_release_review(
                control_plane_root=self.root, record_store=self.store, profile=profile()
            )
            self.assertIsNotNone(review.checklist)
            self.assertEqual(
                set(revoked), {"example/site", "example/shared-addons", "example/disable-online"}
            )
            self.assertEqual(len(revoked), len(set(revoked)))
            revoked.clear()
            self.github.fail = True
            failed = current_release_review(
                control_plane_root=self.root, record_store=self.store, profile=profile()
            )
            self.assertFalse(failed.approved)
            self.assertFalse(failed.checklist_complete)
            self.assertIsNotNone(failed.checklist)
            self.assertNotIn("private provider error", str(failed))
            self.assertEqual(
                set(revoked), {"example/site", "example/shared-addons", "example/disable-online"}
            )
            self.assertEqual(len(revoked), len(set(revoked)))
        with (
            patch(
                "control_plane.release_review.resolve_launchplane_github_token",
                side_effect=lambda **kwargs: (
                    "" if kwargs["repository"] != profile().repository else "website"
                ),
            ),
            patch(
                "control_plane.release_review.github_api_request",
                side_effect=lambda **kwargs: self.github.read(kwargs["path"]),
            ),
        ):
            self.github.fail = False
            failed = current_release_review(
                control_plane_root=self.root, record_store=self.store, profile=profile()
            )
            self.assertFalse(failed.checklist_complete)
            self.assertIsNotNone(failed.checklist)

    def test_unreadable_or_non_forward_shared_sources_keep_manual_review_path(self) -> None:
        for fail, status in ((True, "ahead"), (False, "behind"), (False, "diverged")):
            with self.subTest(fail=fail, status=status):
                self.github.fail, self.github.status = fail, status
                review = self.review()
                assert review.checklist is not None
                self.assertFalse(review.checklist_complete)
                self.assertTrue(review.checklist.additional_changes)
                override = ReleaseReviewDecisionRecord(
                    record_id=f"manual-{fail}-{status}",
                    product=profile().product,
                    checklist_digest=review.checklist_digest,
                    checklist=review.checklist,
                    decision="overridden",
                    reason="Admin reviewed the exact shared changes.",
                    actor_github_id="9003",
                    actor_github_login="operator",
                    decided_at="2026-10-05T00:00:00Z",
                    release_issue_url="https://github.com/example/site/issues/91",
                )
                self.store.write_release_review_decision_record(override)
                self.assertTrue(self.review().approved)
                self.assertEqual(self.store.list_deployment_records(), ())

    def test_historical_shared_override_does_not_approve_new_coverage(self) -> None:
        review = self.review()
        assert review.checklist is not None
        legacy = review.checklist.model_copy(
            update={
                "shared_sources": (),
                "additional_changes": ("Shared components changed; admin review required.",),
            }
        )
        self.store.write_release_review_decision_record(
            ReleaseReviewDecisionRecord(
                record_id="historical-shared-override",
                product=profile().product,
                checklist_digest=checklist_digest(legacy),
                checklist=legacy,
                decision="overridden",
                reason="Reviewed the historical shared-input checklist.",
                actor_github_id="9003",
                actor_github_login="operator",
                decided_at="2026-10-05T00:00:00Z",
                release_issue_url="https://github.com/example/site/issues/91",
            )
        )
        self.assertFalse(self.review().approved)
        self.assertTrue(self.review().checklist_complete)

    def test_historical_website_only_shape_and_annotations_preserve_digest(self) -> None:
        self.set_sources("testing", BASE)
        review = self.review()
        assert review.checklist is not None
        self.assertNotIn("shared_sources", review.checklist.model_dump(mode="json"))
        old = review.checklist.model_dump(mode="json")
        restored = type(review.checklist).model_validate(old)
        self.assertEqual(checklist_digest(restored), review.checklist_digest)
        self.set_sources("testing", HEAD)
        shared = self.review().checklist
        assert shared is not None
        annotated = shared.model_copy(
            update={
                "shared_sources": tuple(
                    source.model_copy(
                        update={
                            "items": tuple(
                                item.model_copy(update={"already_reviewed": True})
                                for item in source.items
                            )
                        }
                    )
                    for source in shared.shared_sources
                )
            }
        )
        self.assertEqual(checklist_digest(shared), checklist_digest(annotated))
