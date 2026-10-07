import unittest
from unittest.mock import Mock, patch

from control_plane.lane_movement import LaneBuild, LaneMovementRefused, require_forward_build
from tests.test_product_reconcile import DEPLOYABLE, OLDER, FakeGitHub, _generic_web_profile


class LaneMovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _generic_web_profile()
        self.store = Mock()
        self.github = FakeGitHub()
        self.current = LaneBuild("current", DEPLOYABLE, "image@sha256:new")

    def test_forward_commit_can_deploy(self) -> None:
        require_forward_build(
            record_store=self.store,
            profile=self.profile,
            current=LaneBuild("old", OLDER),
            desired=self.current,
            transport=self.github,
        )

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
                before.source_build.run_id = after.source_build.run_id = 20
                before.source_build.run_attempt = 2
                after.source_build.run_attempt = after_attempt
                self.store.read_artifact_manifest.side_effect = [before, after]
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
