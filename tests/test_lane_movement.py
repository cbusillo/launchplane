import unittest
from unittest.mock import Mock, patch

from control_plane.lane_movement import LaneBuild, LaneMovementRefused, require_forward_build
from tests.test_product_reconcile import (
    DEPLOYABLE,
    OLDER,
    FakeGenericWebGitHub,
    _generic_web_profile,
    _digest,
)


class LaneMovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _generic_web_profile()
        self.store = Mock()
        self.store.read_artifact_manifest.side_effect = FileNotFoundError
        self.github = FakeGenericWebGitHub()
        self.current = LaneBuild(
            "current", DEPLOYABLE, f"{self.profile.image.repository}@{_digest(DEPLOYABLE)}"
        )

    def test_forward_commit_can_deploy(self) -> None:
        self.github.add_run(10, OLDER)
        self.github.add_run(20, DEPLOYABLE)
        require_forward_build(
            record_store=self.store,
            profile=self.profile,
            current=LaneBuild("old", OLDER, f"{self.profile.image.repository}@{_digest(OLDER)}"),
            desired=self.current,
            transport=self.github,
        )

    def test_descendant_claim_cannot_deploy_an_older_image_or_provider_tag(self) -> None:
        self.github.add_run(10, OLDER)
        self.github.add_run(20, DEPLOYABLE)
        old_image = f"{self.profile.image.repository}@{_digest(OLDER)}"
        for image, deploy_reference in (
            (old_image, ""),
            (self.current.image, f"{self.profile.image.repository}:sha-{OLDER}"),
        ):
            with (
                self.subTest(image=image, tag=deploy_reference),
                self.assertRaises(LaneMovementRefused) as caught,
            ):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=LaneBuild("old", OLDER, old_image),
                    desired=LaneBuild(
                        "candidate", DEPLOYABLE, image, deploy_reference=deploy_reference
                    ),
                    transport=self.github,
                )
            self.assertEqual(caught.exception.code, "build_identity_unverified")

    def test_preview_caller_provenance_cannot_pair_new_source_with_old_odoo_image(self) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.lane_movement import require_forward_preview_build
        from tests.test_product_reconcile import FakeGitHub, PR_HEAD, _profile

        profile = LaunchplaneProductProfileRecord.model_validate(_profile())
        github = FakeGitHub()
        github.add_run(20, PR_HEAD, event="pull_request")
        current = LaneBuild("serving", DEPLOYABLE, self.current.image, pull_request_number=5)
        desired = LaneBuild(
            "claimed", PR_HEAD, f"{profile.image.repository}@{_digest(OLDER)}", source_build=Mock()
        )
        with (
            patch("control_plane.lane_movement.current_preview_builds", return_value=(current,)),
            self.assertRaises(LaneMovementRefused) as caught,
        ):
            require_forward_preview_build(
                record_store=self.store,
                profile=profile,
                preview_slug="existing-preview",
                desired=desired,
                pull_request_number=5,
                transport=github,
            )
        self.assertEqual(caught.exception.code, "build_identity_unverified")

    def test_ancestor_is_refused_even_when_it_has_a_later_build(self) -> None:
        self.github.add_run(40, OLDER)
        with self.assertRaises(LaneMovementRefused) as caught:
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=self.current,
                desired=LaneBuild("older", OLDER),
                transport=self.github,
            )
        self.assertEqual(caught.exception.code, "ancestor_build")

    def test_incomplete_comparison_cannot_authorize_a_change(self) -> None:
        with (
            patch.object(self.github, "get_json", return_value={"status": "ahead"}),
            self.assertRaises(LaneMovementRefused),
        ):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("old", OLDER),
                desired=self.current,
                transport=self.github,
            )

    def test_same_commit_rebuild_requires_newer_provenance(self) -> None:
        for after_attempt in (1, 3):
            with self.subTest(after_attempt=after_attempt):
                before, after = Mock(), Mock()
                before.source_build.repository = after.source_build.repository = (
                    self.profile.repository
                )
                before.source_commit = after.source_commit = DEPLOYABLE
                before.source_build.run_id = after.source_build.run_id = 20
                before.source_build.run_attempt = 2
                after.source_build.run_attempt = after_attempt
                self.store.read_artifact_manifest.side_effect = lambda artifact_id: (
                    before if artifact_id == self.current.artifact_id else after
                )
                if after_attempt == 1:
                    with self.assertRaises(LaneMovementRefused) as caught:
                        require_forward_build(
                            record_store=self.store,
                            profile=self.profile,
                            current=self.current,
                            desired=LaneBuild("other", DEPLOYABLE),
                            transport=self.github,
                        )
                    self.assertEqual(caught.exception.code, "older_artifact")
                else:
                    require_forward_build(
                        record_store=self.store,
                        profile=self.profile,
                        current=self.current,
                        desired=LaneBuild("other", DEPLOYABLE),
                        transport=self.github,
                    )

    def test_missing_same_commit_build_history_is_refused(self) -> None:
        self.store.read_artifact_manifest.side_effect = FileNotFoundError
        with self.assertRaises(LaneMovementRefused):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=self.current,
                desired=LaneBuild("other", DEPLOYABLE),
                transport=self.github,
            )

    def test_newer_source_cannot_replace_a_later_rebuilt_artifact_with_an_older_one(self) -> None:
        before, after = Mock(), Mock()
        before.repository = after.repository = self.profile.repository
        before.run_id, before.run_attempt = 50, 1
        after.run_id, after.run_attempt = 40, 1
        with self.assertRaises(LaneMovementRefused) as caught:
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("before", OLDER, source_build=before),
                desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                transport=self.github,
            )
        self.assertEqual(caught.exception.code, "older_artifact")

    def test_diverged_preview_and_explicit_stable_build_require_newer_artifacts(
        self,
    ) -> None:
        before, after = Mock(), Mock()
        before.repository = after.repository = self.profile.repository
        before.run_id, before.run_attempt = 40, 1
        after.run_id, after.run_attempt = 50, 1
        comparison = {"status": "diverged", "base_commit": {"sha": OLDER}}
        with patch.object(self.github, "get_json", return_value=comparison):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("before", OLDER, source_build=before, pull_request_number=5),
                desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                transport=self.github,
            )
            after.run_id = 30
            with self.assertRaises(LaneMovementRefused):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=LaneBuild("before", OLDER, source_build=before),
                    desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                    transport=self.github,
                )

    def test_legacy_preview_same_commit_rebuild_needs_verified_later_start_time(self) -> None:
        after = Mock(repository=self.profile.repository, run_id=50, run_attempt=2)
        for started, allowed in (("2026-10-01T11:00:00Z", False), ("2026-10-01T13:00:00Z", True)):
            with (
                self.subTest(started=started),
                patch.object(
                    self.github,
                    "get_json",
                    return_value={
                        "id": 50,
                        "run_attempt": 2,
                        "head_sha": DEPLOYABLE,
                        "conclusion": "success",
                        "run_started_at": started,
                    },
                ),
            ):

                def require() -> None:
                    require_forward_build(
                        record_store=self.store,
                        profile=self.profile,
                        current=LaneBuild(
                            "before",
                            DEPLOYABLE,
                            observed_at="2026-10-01T12:00:00Z",
                            pull_request_number=5,
                        ),
                        desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                        transport=self.github,
                    )

                if allowed:
                    require()
                else:
                    with self.assertRaises(LaneMovementRefused):
                        require()
