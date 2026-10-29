"""Shared-source release coverage, including a website plus two addon repositories."""

import unittest
from github_plan_status.client_test_notes import NOTHING_TO_TEST
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.artifact_identity import ArtifactAddonSelector, ArtifactAddonSource
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
)
from control_plane.contracts.repo_product_mapping_read_model import (
    build_repo_product_mapping_service_payload,
)
from control_plane.release_review import (
    build_release_review,
    checklist_blockers,
    checklist_digest,
    current_release_review,
    release_version,
    ReleaseReviewStore,
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

    def change_build_input(self, kind: str, *, source_ref: str = HEAD) -> str:
        artifact = self.store.read_artifact_manifest("artifact-testing")
        provenance = artifact.build_provenance
        inputs = getattr(provenance, kind)
        repository = str(inputs[0].source_repository)
        updates = {"source_ref": source_ref}
        if not source_ref:
            updates["source_repository"] = ""
            if kind == "build_tools":
                updates["version"] = "unverified"
        changed = inputs[0].model_copy(update=updates)
        self.store.write_artifact_manifest(
            artifact.model_copy(
                update={
                    "build_provenance": provenance.model_copy(update={kind: (changed, *inputs[1:])})
                }
            )
        )
        return repository

    def test_build_input_ranges_and_notes_bind_acceptance_without_changing_release_identity(
        self,
    ) -> None:
        from typing import cast

        store = cast(ReleaseReviewStore, cast(object, self.store))
        for kind in ("base_images", "build_tools"):
            with self.subTest(kind=kind):
                seed(self.store)
                version = release_version(store=store, profile=profile(), instance="testing")
                repository = self.change_build_input(kind)
                self.assertEqual(
                    release_version(store=store, profile=profile(), instance="testing"),
                    version,
                )
                accepted = self.accept()
                self.assertTrue(self.review().approved)
                ranges = accepted.checklist.shared_sources
                self.assertEqual([source.repository for source in ranges], [repository])
                self.assertEqual(
                    (ranges[0].production_commit, ranges[0].candidate_commit), (BASE, HEAD)
                )
                self.assertIn(self.github.notes, release_decision_issue_body(accepted))
                self.github.notes += " Check the second sign-in."
                self.assertFalse(self.review().approved)
                self.github.notes = ""
                self.assertFalse(self.review().checklist_complete)
                self.github.notes = f"{NOTHING_TO_TEST}: Build maintenance only."
                self.accept()
                self.assertTrue(self.review().approved)

    def test_product_repository_inputs_must_match_reviewed_product_commits(self) -> None:
        for kind in ("addon_sources", "base_images", "build_tools"):
            for candidate_ref in (HEAD, BASE, "main"):
                with self.subTest(kind=kind, candidate_ref=candidate_ref):
                    seed(self.store)
                    for instance, source_ref in (("prod", BASE), ("testing", candidate_ref)):
                        artifact = self.store.read_artifact_manifest(f"artifact-{instance}")
                        if kind == "addon_sources":
                            updated = artifact.model_copy(
                                update={
                                    "addon_sources": (
                                        *artifact.addon_sources,
                                        ArtifactAddonSource(
                                            repository=profile().repository, ref=source_ref
                                        ),
                                    )
                                }
                            )
                        else:
                            provenance = artifact.build_provenance
                            inputs = getattr(provenance, kind)
                            source = inputs[0].model_copy(
                                update={
                                    "source_repository": profile().repository,
                                    "source_ref": source_ref,
                                }
                            )
                            updated = artifact.model_copy(
                                update={
                                    "build_provenance": provenance.model_copy(
                                        update={kind: (source, *inputs[1:])}
                                    )
                                }
                            )
                        self.store.write_artifact_manifest(updated)
                    self.assertEqual(self.review().checklist_complete, candidate_ref == HEAD)
                    self.accept()
                    self.assertEqual(self.review().approved, candidate_ref == HEAD)

    def test_unknown_build_input_preserves_known_ranges_and_repository_discovery(self) -> None:
        from control_plane.client_release_repositories import client_release_profiles
        from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
        from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
        from typing import Any, cast

        for kind in ("base_images", "build_tools"):
            for source_ref in ("", "main"):
                with self.subTest(kind=kind, source_ref=source_ref):
                    seed(self.store)
                    self.set_sources("prod", BASE)
                    self.set_sources("testing", HEAD)
                    repository = self.change_build_input(kind, source_ref=source_ref)
                    review = self.review()
                    assert review.checklist is not None
                    self.assertFalse(review.checklist_complete)
                    self.assertIn(
                        "example/shared-addons",
                        {source.repository for source in review.checklist.shared_sources},
                    )
                    self.assertEqual(
                        client_release_profiles(
                            store=self.store, repository="example/shared-addons"
                        ),
                        (profile(),),
                    )
                    self.assertEqual(
                        client_release_profiles(store=self.store, repository=repository),
                        (profile(),),
                    )
                    reader = GitHubMergeTrainSnapshotReader(
                        transport=RecordingMergeTrainGitHubTransport(responses=()),
                        branch_refresh_store=cast(Any, self.store),
                    )
                    self.assertTrue(
                        reader.client_review_intent(labels=(), repository=repository, body="").error
                    )
                    self.accept()
                    self.assertFalse(self.review().approved)

    def test_build_input_discovery_uses_recorded_repositories_and_normalizes_them(self) -> None:
        from control_plane.client_release_repositories import client_release_profiles

        for kind in ("base_images", "build_tools"):
            repository = self.change_build_input(kind)
            self.assertEqual(
                client_release_profiles(
                    store=self.store, repository=f"https://github.com/{repository}.git"
                ),
                (profile(),),
            )
            self.assertEqual(
                client_release_profiles(
                    store=self.store, repository=f"git@github.com:{repository}.git"
                ),
                (profile(),),
            )

    def test_shared_repository_reused_by_build_roles_renders_one_range(self) -> None:
        for instance, sha in (("prod", BASE), ("testing", HEAD)):
            artifact = self.store.read_artifact_manifest(f"artifact-{instance}")
            provenance = artifact.build_provenance
            self.store.write_artifact_manifest(
                artifact.model_copy(
                    update={
                        "build_provenance": provenance.model_copy(
                            update={
                                "base_images": tuple(
                                    image.model_copy(
                                        update={
                                            "source_repository": "example/shared-addons",
                                            "source_ref": sha,
                                        }
                                    )
                                    for image in provenance.base_images
                                ),
                                "build_tools": tuple(
                                    tool.model_copy(
                                        update={
                                            "source_repository": "example/shared-addons",
                                            "source_ref": sha,
                                        }
                                    )
                                    for tool in provenance.build_tools
                                ),
                            }
                        )
                    }
                )
            )
        review = self.review()
        assert review.checklist is not None
        self.assertTrue(review.checklist_complete)
        ranges = [
            source
            for source in review.checklist.shared_sources
            if source.repository == "example/shared-addons"
        ]
        self.assertEqual(len(ranges), 1)

    def test_current_manifest_reviews_distinct_pins_from_one_repository(self) -> None:
        from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
        from control_plane.release_review_shared import read_shared_source_changes
        from tests.support.artifact_manifests import artifact_manifest_v2

        production = artifact_manifest_v2()
        payload = production.model_dump(mode="json")
        for image in payload["build_provenance"]["base_images"]:
            image["source_ref"] = HEAD
        payload["build_provenance"]["build_tools"][0]["source_ref"] = HEAD
        payload["dependency_provenance"]["uv_locks"][0]["source_ref"] = HEAD
        candidate = ArtifactIdentityManifest.model_validate(payload)
        ranges, unknown = read_shared_source_changes(
            production=production,
            candidate=candidate,
            repository=profile().repository,
            read=self.github.read,
        )
        self.assertEqual(unknown, ())
        expected = {
            (image.source_repository, image.source_ref, HEAD)
            for image in production.build_provenance.base_images
        } | {
            (tool.source_repository, tool.source_ref, HEAD)
            for tool in production.build_provenance.build_tools
        }
        self.assertEqual(
            {
                (source.repository, source.production_commit, source.candidate_commit)
                for source in ranges
            },
            expected,
        )
        self.assertTrue(
            all(source.items[0].owner_test_notes == self.github.notes for source in ranges)
        )

    def test_production_only_provenance_gap_blocks_acceptance_with_candidate_ranges_visible(
        self,
    ) -> None:
        artifact = self.store.read_artifact_manifest("artifact-prod")
        self.store.write_artifact_manifest(
            artifact.model_copy(update={"build_provenance": type(artifact.build_provenance)()})
        )
        review = self.review()
        assert review.checklist is not None
        self.assertFalse(review.checklist_complete)
        self.assertTrue(review.checklist.additional_changes)
        self.assertTrue(review.checklist.shared_sources)
        self.accept()
        self.assertFalse(self.review().approved)

    def test_overlapping_ranges_do_not_repeat_blockers(self) -> None:
        checklist = self.review().checklist
        assert checklist is not None
        original = checklist.shared_sources[0]
        source = original.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"owner_test_notes": ""}) for item in original.items
                )
            }
        )
        repeated = source.model_copy(update={"production_commit": "d" * 40})
        overlapping = checklist.model_copy(update={"shared_sources": (source, repeated)})
        single = checklist.model_copy(update={"shared_sources": (source,)})
        self.assertEqual(
            checklist_blockers(overlapping, profile=profile()),
            checklist_blockers(single, profile=profile()),
        )

    def test_absent_build_provenance_is_unknown_and_cannot_reuse_acceptance(self) -> None:
        self.accept()
        artifact = self.store.read_artifact_manifest("artifact-testing")
        self.store.write_artifact_manifest(
            artifact.model_copy(update={"build_provenance": type(artifact.build_provenance)()})
        )
        review = self.review()
        self.assertFalse(review.approved)
        self.assertFalse(review.checklist_complete)
        assert review.checklist is not None
        self.assertTrue(review.checklist.shared_sources)

    def test_website_and_two_shared_ranges_are_reviewable_and_persisted(self) -> None:
        review = self.review()
        assert review.checklist is not None
        self.assertEqual(len(review.checklist.items), len(range(1, 30)))
        self.assertEqual(
            {source.repository for source in review.checklist.shared_sources},
            {"example/shared-addons", "example/disable-online"},
        )
        self.assertEqual(checklist_blockers(review.checklist, profile=profile()), ())
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

    def test_shared_engineering_notes_allow_release_and_empty_reasons_block(self) -> None:
        self.github.notes = "Nothing for the Client to test: Shared CI maintenance only."
        accepted = self.accept()
        self.assertTrue(self.review().approved)
        self.assertTrue(accepted.checklist.shared_sources)
        self.github.notes = "Nothing for the Client to test:"
        review = self.review()
        self.assertFalse(review.approved)
        self.assertTrue(any("nonempty reason" in blocker for blocker in review.blockers))

    def test_legacy_acceptance_keeps_its_product_and_shared_notes_binding(self) -> None:
        self.github.notes = "Nothing for the Client to test. Shared dependency update."
        current = self.review()
        assert current.checklist is not None
        self.assertFalse(current.checklist_complete)
        payload = current.checklist.model_dump(mode="json")
        for group in (payload, *payload["shared_sources"]):
            for item in group["items"]:
                item.pop("client_test_notes_error", None)
        legacy = ReleaseChecklist.model_validate(payload)
        accepted = ReleaseReviewDecisionRecord(
            record_id="legacy-shared-acceptance",
            product=profile().product,
            checklist_digest=checklist_digest(legacy),
            checklist=legacy,
            decision="accepted",
            actor_github_id=profile().owner.github_id,
            actor_github_login=profile().owner.github_login,
            decided_at="2026-10-05T00:00:00Z",
            release_issue_url="https://github.com/example/site/issues/90",
        )
        self.store.write_release_review_decision_record(accepted)
        repeated = self.review()
        self.assertTrue(repeated.approved)
        self.assertEqual(repeated.checklist_digest, accepted.checklist_digest)
        self.github.notes += " Changed instructions."
        self.assertFalse(self.review().approved)

    def test_shared_release_membership_comes_from_recorded_artifact_sources(self) -> None:
        from control_plane.client_release_repositories import client_release_profiles
        from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
        from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
        from typing import Any, cast

        self.assertEqual(
            client_release_profiles(store=self.store, repository="EXAMPLE/shared-addons"),
            (profile(),),
        )
        self.assertEqual(
            client_release_profiles(store=self.store, repository="example/unrelated"), ()
        )
        other = profile().model_copy(update={"product": "site-next-version"})
        self.store.write_product_profile_record(other)
        mapping = cast(
            dict[str, Any],
            build_repo_product_mapping_service_payload(
                generated_at="2026-10-29T12:00:00Z",
                product_store=self.store,
                work_request_store=self.store,
            )["mapping"],
        )
        references = mapping["ambiguous_repository_product_references"]
        self.assertEqual(
            references,
            [{"repository": profile().repository, "products": [profile().product, other.product]}],
        )
        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(responses=()),
            branch_refresh_store=cast(Any, self.store),
        )
        self.assertTrue(
            reader.client_review_intent(
                labels=(), repository="example/shared-addons", body=""
            ).error
        )
        real = reader.client_review_intent(
            labels=(),
            repository="example/shared-addons",
            body="## Client test notes\nTry staff sign-in.",
        )
        self.assertFalse(real.error)
        self.assertFalse(real.review_required)
        self.assertFalse(
            reader.client_review_intent(
                labels=(),
                repository="example/shared-addons",
                body="## Client test notes\nNothing for the Client to test: CI only.",
            ).error
        )

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
        self.assertEqual(checklist_blockers(review.checklist, profile=profile()), ())

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
